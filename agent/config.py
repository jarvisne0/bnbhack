"""Static config + universe for the BNB Hack Track-1 agent.

All tunables live here (frozen dataclass — no hidden state). The market is efficient
(8 edges tested, all ~0), so the edge is the risk engine, not a signal: hard DD breaker
+ per-token concentration cap make a single rug or a drawdown spiral unable to breach the
30% DQ line. Everything is stdlib-only and deterministic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# Stables we treat as the safe leg. USDT is the settlement asset (resolves by symbol in twak).
STABLES = ("USDT", "USDC")
# Liquid ballast — lower variance than memes, still eligible BEP-20.
MAJORS = ("BNB", "ETH")
# Tradeable BSC-only high-variance vehicles (vol > $500k/24h, see data/meme_pools.csv).
HIGHVOL = ("SKYAI", "BANANAS31", "TAG", "SIREN", "MYX", "DEXE")

SETTLEMENT = "USDT"

# Pinned CMC ids for the meme universe (resolve by id, not symbol — TAG/SIREN tickers collide).
CMC_IDS = {
    "SKYAI": 36300, "BANANAS31": 34118, "TAG": 34958,
    "SIREN": 35766, "MYX": 36410, "DEXE": 7326,
}


def load_contracts() -> dict[str, str]:
    """BSC contract addresses for tokens twak can't resolve by symbol."""
    return json.loads((DATA / "token_contracts.json").read_text())


@dataclass(frozen=True)
class Config:
    # --- HARD risk invariants (non-negotiable; protect the $24k) ---
    dd_stop: float = 0.25       # rotate ALL to USDT at >=25% drawdown (5% under the 30% DQ)
    max_token: float = 0.25     # per-token cap: a full rug (-100%) => <=25% hit, under 30%
    stable_floor: float = 0.20  # always hold >=20% USDT (dry powder + DD buffer)
    slip: float = 0.02          # abort a swap whose quoted slippage exceeds this
    min_swap: float = 5.0       # skip dust trades (fees/churn)

    # --- behaviour (tunable with user) ---
    aggression: float = 0.60    # target risk-on fraction (capped at 1 - stable_floor)
    n_vehicles: int = 4         # spread convexity across this many names (never all-in one)
    cadence_h: int = 4          # rebalance cadence; guarantees >=1 trade/day (comp needs 7/wk)
    cooldown_h: int = 12        # after a breaker trip, stay in USDT this long

    # --- per-token slippage overrides for thin memes ---
    slip_overrides: dict[str, float] = field(default_factory=lambda: {
        "SIREN": 0.04, "MYX": 0.04, "DEXE": 0.04,
    })

    def __post_init__(self):
        assert 0 < self.dd_stop < 0.30, "dd_stop must sit under the 30% DQ line"
        assert 0 < self.max_token <= 0.30, "a single token must not be able to breach 30%"
        assert 0 <= self.stable_floor < 1
        assert 0 <= self.aggression <= 1 - self.stable_floor, "aggression must leave the stable floor"
        assert self.n_vehicles >= 1

    def slip_for(self, token: str) -> float:
        return self.slip_overrides.get(token, self.slip)
