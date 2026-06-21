#!/usr/bin/env python3
"""Does FUNDING RATE (differentiated positioning data) carry cross-sectional alpha
where price-TA gave zero IC? Funding = who's crowded: very negative funding (shorts
pay longs) often precedes squeeze-up; high positive (longs pay) precedes fade. Pull
Binance 8h funding history for the eligible∩perp subset, align to forward returns,
measure IC + backtest a long-only funding-tilt basket under the 30% DD gate.
No-lookahead: funding[t] decided, return measured t→t+H.
"""
import time, requests, numpy as np, pandas as pd

PX = pd.read_parquet("data/prices_1h.parquet").sort_index()
STABLES = {"USDT","USDC","DAI","FRAX","FDUSD","TUSD","USDe","USDD","USD1","EURI",
           "XUSD","USDf","USDF","FRXUSD","DUSD","lisUSD","STABLE"}
RISK = [c for c in PX.columns if c not in STABLES]
BN = "https://fapi.binance.com"
TX_COST = 0.001; DD_CAP = 0.30; WEEK = 168

def perp_syms():
    r = requests.get(f"{BN}/fapi/v1/exchangeInfo", timeout=30).json()
    return {s["baseAsset"].upper() for s in r["symbols"]
            if s.get("contractType")=="PERPETUAL" and s["quoteAsset"]=="USDT" and s["status"]=="TRADING"}

def funding(sym, n=400):
    out=[]; end=None; need=n
    while need>0:
        u=f"{BN}/fapi/v1/fundingRate?symbol={sym}USDT&limit=1000"
        if end: u+=f"&endTime={end}"
        k=requests.get(u,timeout=20).json()
        if not isinstance(k,list) or not k: break
        out=k+out; end=k[0]["fundingTime"]-1; need-=len(k)
        if len(k)<1000: break
        time.sleep(0.1)
    if not out: return None
    return pd.Series({pd.to_datetime(x["fundingTime"],unit="ms",utc=True):float(x["fundingRate"]) for x in out}).sort_index()

def weekly(r):
    rets,dds=[],[]
    for s in range(0,len(r)-WEEK,6):
        seg=r.iloc[s:s+WEEK]; e=(1+seg).cumprod()
        rets.append(e.iloc[-1]-1); dds.append((e/e.cummax()-1).min())
    return np.array(rets),np.array(dds)

# ---- gather funding for the perp-listed subset of our universe ----
have_perp = perp_syms()
toks = [t for t in RISK if t.upper() in have_perp]
print(f"universe risk={len(RISK)} | with Binance perp+funding={len(toks)}")
fund = {}
for t in toks:
    f = funding(t)
    if f is not None and len(f) > 50: fund[t] = f
F = pd.DataFrame(fund).sort_index()          # 8h funding per token
print(f"funding panel: {F.shape} {F.index.min().date()}→{F.index.max().date()}")

# align funding to forward returns at the same 8h grid
px8 = PX[list(F.columns)].reindex(F.index, method="ffill")
fwd8  = px8.shift(-1)/px8 - 1                 # next 8h return
fwd24 = px8.shift(-3)/px8 - 1                 # next 24h return

def xs_ic(feat, fwd):
    df = pd.DataFrame({"f":feat.stack(),"r":fwd.stack()}).dropna()
    ics = df.groupby(level=0).apply(lambda g: g["f"].corr(g["r"],method="spearman") if len(g)>4 else np.nan).dropna()
    return ics

print("\n=== cross-sectional IC of FUNDING vs forward return ===")
for lbl,fwd in [("fwd 8h",fwd8),("fwd 24h",fwd24)]:
    ic = xs_ic(F, fwd)
    print(f"  raw funding → {lbl}: IC mean={ic.mean():+.4f} t={ic.mean()/ (ic.std()/len(ic)**0.5):+.1f} hit={(ic<0).mean()*100:.0f}%neg (n={len(ic)})")
    # hypothesis: negative funding predicts UP → expect NEGATIVE IC (low funding→high ret)

# ---- funding-tilt long-only basket: long the k most-negative-funding tokens ----
print("\n=== funding-tilt backtest (long k most-NEGATIVE funding, 8h hold) under 30% DD ===")
RET1h = PX[list(F.columns)].pct_change()
for k in [5,8,12]:
    w = pd.DataFrame(0.0, index=PX.index, columns=list(F.columns))
    cols={c:i for i,c in enumerate(w.columns)}
    for ts in F.index[:-1]:
        row = F.loc[ts].dropna()
        if len(row) < k: continue
        longs = row.nsmallest(k).index          # most negative funding
        nxt = F.index[F.index.get_loc(ts)+1] if F.index.get_loc(ts)+1 < len(F.index) else PX.index[-1]
        w.loc[(w.index>=ts)&(w.index<nxt), [cols[c] for c in longs]] = 1.0/k
    wl=w.shift(1).fillna(0); turn=wl.diff().abs().sum(axis=1).fillna(0)
    r=(wl*RET1h).sum(axis=1)-turn*TX_COST
    rets,dds=weekly(r.dropna())
    print(f"  k={k:2d} long-neg-funding | wk_med={np.median(rets)*100:+5.1f}% mean={rets.mean()*100:+5.1f}% "
          f"p90={np.percentile(rets,90)*100:+5.1f}% DQ={(dds<-DD_CAP).mean()*100:.1f}%")
