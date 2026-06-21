#!/usr/bin/env python3
"""Measure CEX→BSC-DEX lead-lag (the LEECH dislocation edge, ported to BSC).
For a liquidity-tiered set of eligible tokens: resolve the best BSC USDT/WBNB pool
(DexScreener), pull minute OHLCV (GeckoTerminal) + matching Binance minute klines,
forward-fill the DEX series, and cross-correlate returns at lags. If BSC lags Binance,
peak correlation sits at a POSITIVE lag = a tradeable convergence signal.
"""
import time, json
import requests, numpy as np, pandas as pd

# eligible tokens across liquidity tiers (all on Binance USDT)
TOKENS = ["ETH","LINK","UNI","AVAX","DOT","LTC","ADA","XRP","DOGE",
          "CAKE","ASTER","TWT","FLOKI","SFP","BAT"]
GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com/latest/dex"
BN = "https://api.binance.com/api/v3"
DAYS = 4

def best_bsc_pool(sym):
    """DexScreener: highest-liquidity BSC pool with stable/WBNB quote."""
    try:
        r = requests.get(f"{DS}/search?q={sym}%20USDT", timeout=20).json()
    except Exception: return None
    cand = []
    for p in r.get("pairs", []) or []:
        if p.get("chainId") != "bsc": continue
        if (p.get("baseToken",{}).get("symbol","").upper() != sym.upper()): continue
        q = p.get("quoteToken",{}).get("symbol","").upper()
        if q not in ("USDT","USDC","WBNB","BUSD"): continue
        liq = float(p.get("liquidity",{}).get("usd",0) or 0)
        cand.append((liq, p["pairAddress"], q, p.get("dexId")))
    if not cand: return None
    cand.sort(reverse=True)
    return cand[0]  # (liq, pool, quote, dex)

def gt_ohlcv(pool, minutes):
    """Minute OHLCV, paginated backward. Returns Series(close) indexed UTC minute."""
    out=[]; before=None; need=minutes
    while need>0:
        url=f"{GT}/networks/bsc/pools/{pool}/ohlcv/minute?limit=1000"
        if before: url+=f"&before_timestamp={before}"
        try: d=requests.get(url,timeout=25).json()
        except Exception: break
        lst=d.get("data",{}).get("attributes",{}).get("ohlcv_list",[])
        if not lst: break
        out=lst+out; before=min(x[0] for x in lst)-1; need-=len(lst)
        time.sleep(2.2)                      # GT free tier ~30/min
        if len(lst)<1000: break
    if not out: return None
    s=pd.Series({pd.to_datetime(x[0],unit="s",utc=True):float(x[4]) for x in out})
    return s.sort_index()

def bn_minutes(sym, minutes):
    out=[]; end=None; need=minutes
    while need>0:
        url=f"{BN}/klines?symbol={sym}USDT&interval=1m&limit=1000"
        if end: url+=f"&endTime={end}"
        k=requests.get(url,timeout=20).json()
        if not isinstance(k,list) or not k: break
        out=k+out; end=k[0][0]-1; need-=len(k)
        if len(k)<1000: break
    s=pd.Series({pd.to_datetime(x[0],unit="ms",utc=True):float(x[4]) for x in out})
    return s.sort_index()

def xcorr(cex, dex, max_lag=10):
    """corr(dex_ret[t], cex_ret[t-lag]); positive lag peak => dex lags cex."""
    idx = cex.index.intersection(dex.index)
    c = np.log(cex.reindex(idx)).diff()
    d = np.log(dex.reindex(idx)).diff()
    df = pd.DataFrame({"c":c,"d":d}).dropna()
    if len(df) < 200: return None, None, len(df)
    best=(0,-9)
    res={}
    for lag in range(-max_lag, max_lag+1):
        v = df["d"].corr(df["c"].shift(lag))
        res[lag]=v
        if v is not None and v>best[1]: best=(lag,v)
    return best, res, len(df)

if __name__=="__main__":
    mins = DAYS*1440
    print(f"CEX→BSC-DEX lead-lag | {DAYS}d minute bars | +lag = DEX lags CEX (tradeable)\n")
    print(f"{'token':6} {'tier($liq)':>12} {'quote':5} {'n_min':>6} {'peak_lag':>8} {'peak_corr':>9} {'corr@0':>7} {'corr@+1':>8}")
    rows=[]
    for sym in TOKENS:
        bp = best_bsc_pool(sym)
        if not bp: print(f"{sym:6} {'-- no BSC pool':>12}"); continue
        liq,pool,quote,dex = bp
        dexs = gt_ohlcv(pool, mins)
        if dexs is None or len(dexs)<200: print(f"{sym:6} {liq:>12,.0f} {quote:5} -- thin/no OHLCV"); continue
        cexs = bn_minutes(sym, mins)
        # forward-fill DEX onto a continuous minute grid spanning the overlap
        grid = pd.date_range(max(dexs.index.min(),cexs.index.min()),
                             min(dexs.index.max(),cexs.index.max()), freq="1min", tz="UTC")
        dexf = dexs.reindex(grid).ffill()
        cexf = cexs.reindex(grid).ffill()
        best,res,n = xcorr(cexf, dexf)
        if best is None: print(f"{sym:6} {liq:>12,.0f} {quote:5} {n:>6} -- too few"); continue
        print(f"{sym:6} {liq:>12,.0f} {quote:5} {n:>6} {best[0]:>+8d} {best[1]:>+9.3f} "
              f"{res.get(0,0):>+7.3f} {res.get(1,0):>+8.3f}")
        rows.append((sym,liq,quote,n,best[0],best[1],res.get(0,0),res.get(1,0)))
        time.sleep(0.5)
    if rows:
        df=pd.DataFrame(rows,columns=["token","liq","quote","n","peak_lag","peak_corr","corr0","corr1"])
        df.to_csv("data/dislocation_xcorr.csv",index=False)
        lag_pos=df[df.peak_lag>0]
        print(f"\nSUMMARY: {len(df)} tokens | DEX-lags-CEX (peak_lag>0): {len(lag_pos)} | "
              f"median peak_corr (lag>0): {lag_pos.peak_corr.median():.3f}" if len(lag_pos) else
              f"\nSUMMARY: {len(df)} tokens | none show DEX lagging CEX at minute res")
