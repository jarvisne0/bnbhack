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

# Stables we treat as the safe leg. USDC is the settlement asset (resolves by symbol in twak).
STABLES = ("USDT", "USDC")
# Liquid ballast — lower variance than memes, still eligible BEP-20.
MAJORS = ("BNB", "ETH")
# Tradeable BSC-only high-variance vehicles (vol > $500k/24h, see data/meme_pools.csv).
HIGHVOL = ("SKYAI", "BANANAS31", "TAG", "SIREN", "MYX", "DEXE")

SETTLEMENT = "USDC"  # the wallet is funded in USDC; the settlement leg must match what's held

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
    max_token: float = 0.27     # per-token entry cap: a full rug (-100%) => <=27% hit, under 30%; clears $1 at ~$4 bankroll
    hard_cap: float = 0.28      # run-time ceiling: trim a winner back to max_token above this; <30% rug guard
    stable_floor: float = 0.20  # always hold >=20% USDT (dry powder + DD buffer)
    slip: float = 0.02          # abort a swap whose quoted slippage exceeds this
    min_swap: float = 1.0       # hackathon rule: every trade must be >= $1 to count

    # --- per-position stops (cheap round-trips make these affordable) ---
    trail: float = 0.15         # exit a position that falls this far from its peak value
    stop_loss: float = 0.12     # exit a position this far underwater from entry

    # --- behaviour (tunable with user) ---
    aggression: float = 0.60    # target risk-on fraction (capped at 1 - stable_floor)
    n_vehicles: int = 1         # ~$4 bankroll + $1 floor + 20% reserve: budget/2 never clears $1 outside Greed (=> all-cash); 1 concentrated slot is the only fillable shape
    cadence_h: int = 2          # rebalance cadence; faster reaction now that trading is ~free
    cooldown_h: int = 12        # after a breaker trip, stay in USDT this long

    # --- per-token slippage overrides for thin memes ---
    slip_overrides: dict[str, float] = field(default_factory=lambda: {
        "SIREN": 0.04, "MYX": 0.04, "DEXE": 0.04,
    })

    def __post_init__(self):
        assert 0 < self.dd_stop < 0.30, "dd_stop must sit under the 30% DQ line"
        assert 0 < self.max_token <= self.hard_cap < 0.30, "no single token may reach the 30% rug line"
        assert 0 <= self.stable_floor < 1
        assert 0 <= self.aggression <= 1 - self.stable_floor, "aggression must leave the stable floor"
        assert 0 < self.trail < 1 and 0 < self.stop_loss < 1
        assert self.n_vehicles >= 1

    def slip_for(self, token: str) -> float:
        return self.slip_overrides.get(token, self.slip)
