#!/usr/bin/env python3
"""Fetch hourly klines for the BNB Hack eligible BEP-20 universe (CEX-listed subset
via Binance). Output: data/prices_1h.parquet (index=hour UTC, cols=token close).
This is the backtest substrate for cross-sectional momentum / lead-lag / regime —
i.e. validating which edge has alpha + survives the 30% DD gate on the real universe.
"""
import time, json, sys
import requests, pandas as pd, numpy as np

# 149 eligible BEP-20 tokens (from the hackathon detail page). Non-ASCII / unlisted
# names are simply skipped when no Binance USDT pair exists.
ELIGIBLE = """ETH USDT USDC XRP TRX DOGE ZEC ADA LINK BCH DAI TON USD1 USDe M LTC AVAX SHIB
XAUt WLFI H DOT UNI ASTER DEXE USDD ETC AAVE ATOM U STABLE FIL INJ NIGHT FET TUSD BONK
PENGU CAKE SIREN LUNC ZRO KITE FDUSD BEAT PIEVERSE BTT NFT EDGE FLOKI LDO B FF PENDLE NEX
STG AXS TWT HOME RAY COMP GWEI XCN GENIUS XPL BAT SKYAI APE IP SFP TAG NXPC AB SAHARA 1INCH
CHEEMS BANANAS31 RIVER MYX RAVE SNX FORM LAB HTX USDf CTM BDX SLX UB DUCKY FRAX BILL WFI KOGE
ALE FRXUSD USDF GOMINING VCNT GUA DUSD SMILEK 0G BEAM MY SOON REAL Q AIOZ ZIG YFI TAC lisUSD
CYS ZAMA TRIA HUMA PLUME ZIL XPR ZETA BabyDoge NILA ROSE VELO UAI BRETT OPEN BSB TOSHI BAS ACH
AXL LUR ELF KAVA APR IRYS EURI XUSD BARD DUSK SUSHI PEAQ COAI BDCA XAUM BNB WBNB""".split()

BASE = "https://api.binance.com"

def usdt_symbols():
    r = requests.get(f"{BASE}/api/v3/exchangeInfo", timeout=30).json()
    live = {s["baseAsset"].upper(): s["symbol"] for s in r["symbols"]
            if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
    return live

def fetch_klines(symbol, hours=2880):
    """Hourly closes, most-recent `hours`. Paginates backward."""
    out = []
    end = None
    need = hours
    while need > 0:
        lim = min(1000, need)
        url = f"{BASE}/api/v3/klines?symbol={symbol}&interval=1h&limit={lim}"
        if end: url += f"&endTime={end}"
        k = requests.get(url, timeout=30).json()
        if not isinstance(k, list) or not k: break
        out = k + out
        end = k[0][0] - 1
        need -= len(k)
        if len(k) < lim: break
        time.sleep(0.12)
    if not out: return None
    df = pd.DataFrame(out, columns=["ot","o","h","l","c","v","ct","qv","n","tb","tq","ig"])
    df["ts"] = pd.to_datetime(df["ot"], unit="ms", utc=True)
    return df.set_index("ts")["c"].astype(float)

def main():
    live = usdt_symbols()
    have = [t for t in dict.fromkeys(ELIGIBLE) if t.upper() in live]
    miss = [t for t in dict.fromkeys(ELIGIBLE) if t.upper() not in live]
    print(f"eligible={len(set(ELIGIBLE))} | binance-USDT listed={len(have)} | unlisted={len(miss)}")
    print("listed:", " ".join(have))
    print("unlisted (BSC-only / non-Binance):", " ".join(miss))
    series = {}
    for i, t in enumerate(have):
        s = fetch_klines(live[t.upper()])
        if s is not None and len(s) > 200:
            series[t] = s
        print(f"  [{i+1}/{len(have)}] {t}: {0 if s is None else len(s)} hrs", flush=True)
    px = pd.DataFrame(series).sort_index()
    px.to_parquet("data/prices_1h.parquet")
    print(f"\nSAVED data/prices_1h.parquet  shape={px.shape}  span={px.index.min()} → {px.index.max()}")

if __name__ == "__main__":
    main()
