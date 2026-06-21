#!/usr/bin/env python3
"""LightGBM cross-sectional model for the BNB Hack universe. Predicts forward 24h
RELATIVE return (vs universe median) from price-derived features; longs top-k; scored
under the comp's rule (hourly returns, 30% DD = DQ, sim costs) across weekly windows.
Strict no-lookahead: time-ordered split with a 24h purge gap (target horizon) so test
features never see train targets. Compares to the mean-rev baseline + leverage frontier.
"""
import numpy as np, pandas as pd, lightgbm as lgb

PX = pd.read_parquet("data/prices_1h.parquet").sort_index()
STABLES = {"USDT","USDC","DAI","FRAX","FDUSD","TUSD","USDe","USDD","USD1","EURI",
           "XUSD","USDf","USDF","FRXUSD","DUSD","lisUSD","STABLE"}
RISK = [c for c in PX.columns if c not in STABLES]
PXR = PX[RISK]
RET = PXR.pct_change()
H = 24                       # forward target horizon (hours)
TX_COST = 0.001; DD_CAP = 0.30; WEEK = 168

def build_panel():
    rows = []
    feats = {}
    for h in [1,6,12,24,72,168]:
        feats[f"ret{h}"] = PXR.pct_change(h)
    feats["vol24"] = RET.rolling(24).std()
    feats["vol72"] = RET.rolling(72).std()
    feats["dist_hi168"] = PXR/PXR.rolling(168).max() - 1
    feats["dist_lo168"] = PXR/PXR.rolling(168).min() - 1
    # cross-sectional ranks (relative position in universe at each hour)
    feats["xsrank24"] = feats["ret24"].rank(axis=1, pct=True)
    feats["xsrank72"] = feats["ret72"].rank(axis=1, pct=True)
    # target: forward H-hr return minus universe median (cross-sectional alpha)
    fwd = PXR.shift(-H)/PXR - 1
    tgt = fwd.sub(fwd.median(axis=1), axis=0)
    # stack to long panel
    panel = {k: v.stack() for k, v in feats.items()}
    panel["y"] = tgt.stack()
    df = pd.DataFrame(panel).dropna()
    df.index.names = ["ts","token"]
    return df

def weekly(r):
    rets, dds = [], []
    for s in range(0, len(r)-WEEK, 6):
        seg=r.iloc[s:s+WEEK]; e=(1+seg).cumprod()
        rets.append(e.iloc[-1]-1); dds.append((e/e.cummax()-1).min())
    return np.array(rets), np.array(dds)

def sim_topk(pred_df, k=8, rebal=24, lev=1.0):
    """pred_df: index=(ts,token) -> predicted alpha. Long top-k each rebal hour."""
    pred = pred_df.unstack("token").reindex(columns=RISK)
    w = pd.DataFrame(0.0, index=PXR.index, columns=RISK).reindex(pred.index)
    cols = {c:i for i,c in enumerate(w.columns)}
    idx = list(w.index)
    for i in range(0, len(idx), rebal):
        row = pred.iloc[i].dropna()
        if len(row) < k: continue
        top = row.nlargest(k).index
        for c in top: w.iloc[i:i+rebal, cols[c]] = lev/k
    rr = RET.reindex(w.index)[w.columns]
    wl = w.shift(1).fillna(0.0)
    turn = wl.diff().abs().sum(axis=1).fillna(0.0)
    return (wl*rr).sum(axis=1) - turn*TX_COST

if __name__ == "__main__":
    df = build_panel()
    times = df.index.get_level_values("ts").unique().sort_values()
    split = times[int(len(times)*0.65)]
    purge = split + pd.Timedelta(hours=H)
    train = df[df.index.get_level_values("ts") <= split]
    test  = df[df.index.get_level_values("ts") >= purge]
    Xcols = [c for c in df.columns if c != "y"]
    print(f"panel rows={len(df)} | train={len(train)} (≤{split.date()}) | test={len(test)} (≥{purge.date()}) | feats={len(Xcols)}")

    model = lgb.LGBMRegressor(n_estimators=400, learning_rate=0.03, num_leaves=31,
                              subsample=0.8, colsample_bytree=0.8, min_child_samples=200,
                              reg_lambda=1.0, n_jobs=4, verbose=-1)
    model.fit(train[Xcols], train["y"])
    imp = sorted(zip(Xcols, model.feature_importances_), key=lambda x:-x[1])
    print("feat importance:", ", ".join(f"{k}={v}" for k,v in imp))

    test = test.copy(); test["pred"] = model.predict(test[Xcols])
    # IC: rank-corr of pred vs realized, per hour
    ic = test.groupby(level="ts").apply(lambda g: g["pred"].corr(g["y"], method="spearman")).dropna()
    print(f"\nOOS rank-IC: mean={ic.mean():+.4f} std={ic.std():.4f} hit={ (ic>0).mean()*100:.0f}% (n={len(ic)} hrs)")

    print("\n=== LGBM top-k=8, rebal=24h — leverage frontier (TEST window only) ===")
    print(f"{'lev':>4} | {'wk_med':>7} {'wk_mean':>7} {'p10':>7} {'p90':>7} {'DQ%':>6}")
    for lev in [1,2,3,4]:
        r = sim_topk(test["pred"], k=8, rebal=24, lev=lev).dropna()
        rets, dds = weekly(r)
        if len(rets)==0: print(f"{lev}x: too few windows"); continue
        print(f"{lev:>3}x | {np.median(rets)*100:>+6.1f}% {rets.mean()*100:>+6.1f}% "
              f"{np.percentile(rets,10)*100:>+6.1f}% {np.percentile(rets,90)*100:>+6.1f}% "
              f"{(dds<-DD_CAP).mean()*100:>5.1f}%")

    # baseline mean-rev on SAME test window for apples-to-apples
    print("\n=== baseline mean-rev (buy 12h losers k8) on SAME test window ===")
    score = PXR.pct_change(12)
    mr = pd.DataFrame(0.0, index=PXR.index, columns=RISK)
    cols={c:i for i,c in enumerate(RISK)}
    for i in range(12,len(PXR),24):
        bot=score.iloc[i].dropna().nsmallest(8).index
        for c in bot: mr.iloc[i:i+24,cols[c]]=1.0/8
    mr=mr.reindex(test.index.get_level_values('ts').unique().sort_values())
    rr=RET.reindex(mr.index)[mr.columns]; wl=mr.shift(1).fillna(0); turn=wl.diff().abs().sum(axis=1).fillna(0)
    r=(wl*rr).sum(axis=1)-turn*TX_COST
    rets,dds=weekly(r.dropna())
    print(f" 1x | wk_med={np.median(rets)*100:+.1f}% mean={rets.mean()*100:+.1f}% DQ={ (dds<-DD_CAP).mean()*100:.1f}%")
