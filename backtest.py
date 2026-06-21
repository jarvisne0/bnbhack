#!/usr/bin/env python3
"""BNB Hack Track-1 alpha hunt. Replicates the scoring: hourly returns, ranked by
TOTAL RETURN, 30% max-DD = DQ, >=1 trade/day, simulated tx costs. Because the comp
is ONE week, we evaluate each strategy over ALL rolling 168h windows and report the
DISTRIBUTION + DQ rate — a strategy that wins one week but blows 30% DD often is dead.
Selection rubric: (1) works (median weekly return), (2) survives DD (low DQ rate),
(3) [stack-fit judged separately].
"""
import numpy as np, pandas as pd

PX = pd.read_parquet("data/prices_1h.parquet").sort_index()
RET = PX.pct_change().fillna(0.0)
STABLES = {"USDT","USDC","DAI","FRAX","FDUSD","TUSD","USDe","USDD","USD1","EURI",
           "XUSD","USDf","USDF","FRXUSD","DUSD","lisUSD","STABLE"}
RISK = [c for c in PX.columns if c not in STABLES]
TX_COST = 0.001          # 0.1% per unit turnover (conservative vs 0.0665% taker)
DD_CAP = 0.30            # >30% intra-window drawdown = DQ
WEEK = 168              # hours

def simulate(weights: pd.DataFrame):
    """weights: hourly target weights (rows=time, cols=token), rows sum<=1 (rest=cash).
    Returns hourly portfolio return series net of turnover tx cost."""
    w = weights.reindex(columns=PX.columns).fillna(0.0).shift(1).fillna(0.0)  # act next hour (no lookahead)
    turnover = w.diff().abs().sum(axis=1).fillna(0.0)
    gross = (w * RET).sum(axis=1)
    return gross - turnover * TX_COST

def windows_stats(r: pd.Series, label):
    eq = (1 + r).cumprod()
    rets, dds = [], []
    idx = r.index
    for s in range(0, len(r) - WEEK, 6):           # step 6h across all 1-week windows
        seg = r.iloc[s:s+WEEK]
        e = (1 + seg).cumprod()
        wret = e.iloc[-1] - 1
        dd = (e / e.cummax() - 1).min()
        rets.append(wret); dds.append(dd)
    rets, dds = np.array(rets), np.array(dds)
    dq = (dds < -DD_CAP).mean()
    survive = rets[dds >= -DD_CAP]
    print(f"{label:34s} | wk_ret med={np.median(rets)*100:+6.1f}% "
          f"mean={rets.mean()*100:+6.1f}% p10={np.percentile(rets,10)*100:+6.1f}% "
          f"p90={np.percentile(rets,90)*100:+6.1f}% | DQ(DD>30%)={dq*100:4.1f}% | "
          f"surv_med={np.median(survive)*100 if len(survive) else 0:+5.1f}%")
    return dict(label=label, med=np.median(rets), mean=rets.mean(), dq=dq)

# ---------- strategies (target weights each hour, decisions from PAST only) ----------
def w_hold(tokens):
    w = pd.DataFrame(0.0, index=PX.index, columns=PX.columns)
    w[tokens] = 1.0/len(tokens)
    return w

def w_xsec_momentum(lookback=72, k=5, rebal=24):
    score = PX[RISK].pct_change(lookback)
    w = pd.DataFrame(0.0, index=PX.index, columns=PX.columns)
    for i in range(lookback, len(PX), rebal):
        row = score.iloc[i].dropna()
        top = row.nlargest(k).index
        w.iloc[i:i+rebal, [w.columns.get_loc(c) for c in top]] = 1.0/k
    return w

def w_vol_managed_momentum(lookback=72, k=5, rebal=24, vol_lb=72, target_vol=0.02):
    base = w_xsec_momentum(lookback, k, rebal)
    pv = RET[RISK].rolling(vol_lb).std().mean(axis=1).bfill()
    scale = (target_vol / pv).clip(0, 1.0)
    return base.mul(scale, axis=0)

def w_meanrev(lookback=24, k=5, rebal=12):
    score = PX[RISK].pct_change(lookback)
    w = pd.DataFrame(0.0, index=PX.index, columns=PX.columns)
    for i in range(lookback, len(PX), rebal):
        row = score.iloc[i].dropna()
        bot = row.nsmallest(k).index            # contrarian: buy biggest losers
        w.iloc[i:i+rebal, [w.columns.get_loc(c) for c in bot]] = 1.0/k
    return w

def w_leadlag(lead="BNB", lookback=12, k=5, rebal=12):
    """When the lead (BNB) is up over lookback, hold top-beta alts; else cash (stables)."""
    lead_ret = PX[lead].pct_change(lookback)
    beta = RET[RISK].rolling(168).corr(RET[lead]).fillna(0) if False else None
    score = PX[RISK].pct_change(lookback)
    w = pd.DataFrame(0.0, index=PX.index, columns=PX.columns)
    for i in range(lookback, len(PX), rebal):
        if lead_ret.iloc[i] > 0:
            top = score.iloc[i].dropna().nlargest(k).index
            w.iloc[i:i+rebal, [w.columns.get_loc(c) for c in top]] = 1.0/k
        # else: stay in cash (all-zero row = cash, 0% return, preserves capital)
    return w

if __name__ == "__main__":
    print(f"universe={PX.shape[1]} tokens ({len(RISK)} risk, {PX.shape[1]-len(RISK)} stable) | "
          f"{len(PX)} hrs {PX.index.min().date()}→{PX.index.max().date()} | "
          f"tx={TX_COST*100:.2f}%/turn | DD_cap={DD_CAP*100:.0f}% | window={WEEK}h\n")
    print(f"{'STRATEGY':34s} |  weekly-return distribution                       | risk")
    res = []
    res.append(windows_stats(simulate(w_hold(["BNB"])),                 "B&H BNB"))
    res.append(windows_stats(simulate(w_hold(["ETH"])),                 "B&H ETH"))
    res.append(windows_stats(simulate(w_hold(RISK)),                    "B&H equal-weight basket"))
    res.append(windows_stats(simulate(w_xsec_momentum(72,5,24)),        "xsec-momentum L72 k5 r24"))
    res.append(windows_stats(simulate(w_xsec_momentum(24,5,12)),        "xsec-momentum L24 k5 r12"))
    res.append(windows_stats(simulate(w_xsec_momentum(168,3,24)),       "xsec-momentum L168 k3 r24"))
    res.append(windows_stats(simulate(w_vol_managed_momentum(72,5,24)), "vol-managed-mom L72 k5"))
    res.append(windows_stats(simulate(w_meanrev(24,5,12)),              "mean-reversion L24 k5 r12"))
    res.append(windows_stats(simulate(w_leadlag("BNB",12,5,12)),        "leadlag BNB->alts L12 k5"))
