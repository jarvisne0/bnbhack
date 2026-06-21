"""CoinMarketCap Pro client — the deterministic data layer (Basic plan, in-credit).

Two load-bearing signals, both numeric (no free-text parsing in the control path):
  - per-token `trending` = 24h volume change %  (volume spiking = the name is heating up)
  - market `fear_greed`  = the CMC Fear & Greed index, logged as regime context

Key is read from env CMC_API_KEY or a 0600 key file — never hard-coded, never logged.
The qualitative Skill-Hub evidence packs (altcoin_kol_sentiment etc.) are a separate
submission/analyst artefact, deliberately kept OUT of the deterministic loop.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

from .config import CMC_IDS

BASE = "https://pro-api.coinmarketcap.com"


def _load_key() -> str:
    key = os.environ.get("CMC_API_KEY")
    if key:
        return key.strip()
    for p in (os.environ.get("CMC_KEY_FILE"), "~/.config/bnbagent/cmc_key"):
        if p and Path(p).expanduser().exists():
            return Path(p).expanduser().read_text().strip()
    raise RuntimeError("no CMC key (set CMC_API_KEY or ~/.config/bnbagent/cmc_key)")


class Cmc:
    def __init__(self, key: str | None = None, timeout: int = 30):
        self.key = key or _load_key()
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        r = requests.get(BASE + path, headers={"X-CMC_PRO_API_KEY": self.key,
                         "Accept": "application/json"}, params=params, timeout=self.timeout)
        j = r.json()
        st = j.get("status", {})
        if int(st.get("error_code") or 0) != 0:  # CMC returns "0" (str) or 0 (int) when OK
            raise RuntimeError(f"CMC {path}: {st.get('error_message')}")
        return j.get("data", {})

    def fear_greed(self) -> dict:
        """{'value': 0-100, 'classification': str} — market regime context."""
        d = self._get("/v3/fear-and-greed/latest", {})
        return {"value": int(d.get("value", 50)), "classification": d.get("value_classification", "")}

    def heat(self, ids: dict[str, int] | None = None) -> dict[str, dict[str, float]]:
        """{token: {'trending': volume_change_24h}} for the pinned meme ids."""
        ids = ids or CMC_IDS
        data = self._get("/v2/cryptocurrency/quotes/latest",
                         {"id": ",".join(str(i) for i in ids.values()), "convert": "USD"})
        by_id = {str(i): tok for tok, i in ids.items()}
        out: dict[str, dict[str, float]] = {}
        for cid, entry in data.items():
            tok = by_id.get(str(cid))
            if not tok:
                continue
            q = (entry.get("quote") or {}).get("USD", {})
            v = q.get("volume_change_24h")
            if v is not None:
                out[tok] = {"trending": float(v)}
        return out
