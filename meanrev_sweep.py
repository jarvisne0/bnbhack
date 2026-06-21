#!/usr/bin/env python3
"""Tune the short-horizon mean-reversion edge (the only spot edge that beat hold with
0% DQ), then map the leverage→(return, DQ) frontier — the 'most profit without blowing
up' sweet spot for the 30% DD gate. Leverage is how a perps build amplifies the edge.
"""
import numpy as np, pandas as pd
from itertools import product

PX = pd.read_parquet("data/prices_1h.parquet").sort_index()
RET = PX.pct_change().fillna(0.0)
STABLES = {"USDT","USDC","DAI","FRAX","FDUSD","TUSD","USDe","USDD","USD1","EURI",
           "XUSD","USDf","USDF","FRXUSD","DUSD","lisUSD","STABLE"}
RISK = [c for c in PX.columns if c not in STABLES]
TX_COST = 0.001; DD_CAP = 0.30; WEEK = 168

def meanrev_weights(lookback, k, rebal, lev=1.0):
    score = PX[RISK].pct_change(lookback)
    w = pd.DataFrame(0.0, index=PX.index, columns=PX.columns)
    cols = {c: w.columns.get_loc(c) for c in RISK}
    for i in range(lookback, len(PX), rebal):
        bot = score.iloc[i].dropna().nsmallest(k).index
        for c in bot:
            w.iloc[i:i+rebal, cols[c]] = lev/k
    return w

def sim(weights):
    w = weights.reindex(columns=PX.columns).fillna(0.0).shift(1).fillna(0.0)
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    return (w*RET).sum(axis=1) - turn*TX_COST

def wstats(r):
    rets, dds = [], []
    for s in range(0, len(r)-WEEK, 6):
        seg = r.iloc[s:s+WEEK]; e = (1+seg).cumprod()
        rets.append(e.iloc[-1]-1); dds.append((e/e.cummax()-1).min())
    rets, dds = np.array(rets), np.array(dds)
    return np.median(rets), rets.mean(), (dds < -DD_CAP).mean(), np.percentile(rets,90)

print("=== mean-reversion param sweep (1x) — find best lookback/k/rebal ===")
print(f"{'lookback':>8} {'k':>3} {'rebal':>5} | {'wk_med':>7} {'wk_mean':>7} {'DQ%':>5} {'p90':>7}")
best = None
for lb, k, rb in product([6,12,24,48],[3,5,8,12],[6,12,24]):
    med, mean, dq, p90 = wstats(sim(meanrev_weights(lb,k,rb)))
    if best is None or (dq < 0.02 and med > best[1]): best = ((lb,k,rb), med, mean, dq)
    if med > 0.012 or (lb,k,rb) in [(24,5,12)]:
        print(f"{lb:>8} {k:>3} {rb:>5} | {med*100:>+6.1f}% {mean*100:>+6.1f}% {dq*100:>4.1f}% {p90*100:>+6.1f}%")

cfg = best[0]
print(f"\n=== leverage frontier @ best config lookback={cfg[0]} k={cfg[1]} rebal={cfg[2]} ===")
print(f"{'lev':>4} | {'wk_med':>7} {'wk_mean':>7} {'p10':>7} {'p90':>7} {'DQ%(>30%DD)':>11}")
for lev in [1,2,3,4,5,6,8]:
    r = sim(meanrev_weights(cfg[0],cfg[1],cfg[2],lev=lev))
    rets, dds = [], []
    for s in range(0, len(r)-WEEK, 6):
        seg=r.iloc[s:s+WEEK]; e=(1+seg).cumprod()
        rets.append(e.iloc[-1]-1); dds.append((e/e.cummax()-1).min())
    rets,dds=np.array(rets),np.array(dds)
    print(f"{lev:>3}x | {np.median(rets)*100:>+6.1f}% {rets.mean()*100:>+6.1f}% "
          f"{np.percentile(rets,10)*100:>+6.1f}% {np.percentile(rets,90)*100:>+6.1f}% "
          f"{(dds<-DD_CAP).mean()*100:>10.1f}%")
