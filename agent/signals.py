"""Live signal gathering for the selector.

Momentum comes straight from twak price history (always available). CMC sentiment/trending,
when provided, is merged in — the selector z-scores whatever metrics are present, so the
agent degrades gracefully if CMC is briefly unavailable. No predictive claim: this ranks
which high-variance names are heating up, for convex vehicle selection.
"""
from __future__ import annotations

from .twakcli import Twak, TwakError


def momentum(tw: Twak, contract: str, period: str = "day") -> float | None:
    """Return over the window = priceUsd / earliest price - 1. None if history is thin."""
    try:
        r = tw.price_history(contract, period)
    except TwakError:
        return None
    hist = r.get("history") or []
    now = r.get("priceUsd")
    if not hist or not now:
        return None
    first = hist[0].get("price")
    if not first:
        return None
    return float(now) / float(first) - 1.0


def gather(tw: Twak, contracts: dict[str, str], cmc: dict[str, dict] | None = None,
           period: str = "day") -> dict[str, dict[str, float]]:
    """Build {token: {momentum, [sentiment, trending]}} for the candidate universe."""
    cmc = cmc or {}
    out: dict[str, dict[str, float]] = {}
    for tok, contract in contracts.items():
        sig: dict[str, float] = {}
        m = momentum(tw, contract, period)
        if m is not None:
            sig["momentum"] = m
        c = cmc.get(tok, {})
        if "sentiment" in c:
            sig["sentiment"] = float(c["sentiment"])
        if "trending" in c:
            sig["trending"] = float(c["trending"])
        if sig:
            out[tok] = sig
    return out
