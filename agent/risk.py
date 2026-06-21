"""Pure risk functions — no I/O, fully deterministic, unit-tested.

These encode the only edge we have: discipline. Given holdings and a high-water mark,
they decide whether to hit the kill-switch, and turn a set of selected vehicles into
concentration-capped convex target weights that no single rug can push past the 30% DQ.
"""
from __future__ import annotations

from .config import Config, SETTLEMENT


def equity(holdings: dict[str, float]) -> float:
    """Total portfolio USD value."""
    return sum(holdings.values())


def drawdown(eq: float, hwm: float) -> float:
    """Fractional drawdown from the high-water mark (0.0 at/above the peak)."""
    if hwm <= 0:
        return 0.0
    return max(0.0, 1.0 - eq / hwm)


def breaker_tripped(eq: float, hwm: float, cfg: Config) -> bool:
    """True once drawdown reaches dd_stop — caller must rotate everything to USDT."""
    return drawdown(eq, hwm) >= cfg.dd_stop


def target_weights(scores: dict[str, float], cfg: Config) -> dict[str, float]:
    """Convex, concentration-capped target allocation (fractions summing to 1.0).

    Positive-scored vehicles share the risk-on budget proportionally, each clamped to
    max_token; the residual (incl. everything dropped by the clamp) lands in USDT, so the
    stable floor is always met and no single position can exceed max_token.
    """
    budget = min(cfg.aggression, 1.0 - cfg.stable_floor)
    pos = {t: s for t, s in scores.items() if s > 0 and t != SETTLEMENT}
    weights: dict[str, float] = {}
    total = sum(pos.values())
    if total > 0:
        for t, s in pos.items():
            weights[t] = min(s / total * budget, cfg.max_token)
    risk_on = sum(weights.values())
    weights[SETTLEMENT] = round(1.0 - risk_on, 10)
    return {t: w for t, w in weights.items() if w > 0}


def rebalance_plan(holdings: dict[str, float], target: dict[str, float],
                   cfg: Config) -> list[tuple[str, str, float]]:
    """Diff current vs target into a deterministic list of (from, to, usd) swaps.

    Everything trades through USDT: sells first (free up settlement), then buys. Deltas
    below min_swap are skipped to avoid dust churn. Order is stable (sorted) for repeatable,
    auditable runs.
    """
    eq = equity(holdings)
    if eq <= 0:
        return []
    desired = {t: target.get(t, 0.0) * eq for t in set(holdings) | set(target)}
    deltas = {t: desired.get(t, 0.0) - holdings.get(t, 0.0) for t in desired}
    sells, buys = [], []
    for t in sorted(deltas):
        if t == SETTLEMENT:
            continue
        d = deltas[t]
        if d < -cfg.min_swap:
            sells.append((t, SETTLEMENT, round(-d, 6)))
        elif d > cfg.min_swap:
            buys.append((SETTLEMENT, t, round(d, 6)))
    return sells + buys


def worst_case_drawdown(target: dict[str, float]) -> float:
    """Largest single-token weight = worst instantaneous loss if one vehicle rugs to zero.

    Used as a guard/assertion: this must stay strictly under the 30% DQ line.
    """
    risk = {t: w for t, w in target.items() if t != SETTLEMENT}
    return max(risk.values(), default=0.0)
