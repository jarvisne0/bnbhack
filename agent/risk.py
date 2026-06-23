"""Pure risk functions — no I/O, fully deterministic, unit-tested.

These encode the only edge we have: discipline. Given holdings and a high-water mark,
they decide whether to hit the kill-switch, and turn a set of selected vehicles into
concentration-capped convex target weights that no single rug can push past the 30% DQ.
"""
from __future__ import annotations

from .config import Config, HIGHVOL, SETTLEMENT


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
        if t not in HIGHVOL:
            continue  # only meme positions rotate; settlement is the cash leg, BNB is gas
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


def track_positions(held: dict[str, float], peaks: dict[str, float],
                    entries: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
    """Carry per-position entry value and running peak, observed from live holdings.

    A token first seen held records its current value as entry; peaks ratchet up; tokens no
    longer held are dropped. Reality-driven, so it stays correct regardless of fill success.
    """
    new_peaks = {t: max(peaks.get(t, v), v) for t, v in held.items()}
    new_entries = {t: entries.get(t, v) for t, v in held.items()}
    return new_peaks, new_entries


def stop_exits(held: dict[str, float], peaks: dict[str, float],
               entries: dict[str, float], cfg: Config) -> set[str]:
    """Positions to liquidate now: fell `trail` from peak, or `stop_loss` underwater from entry."""
    out = set()
    for t, v in held.items():
        pk, en = peaks.get(t, v), entries.get(t, v)
        if (pk > 0 and v <= pk * (1 - cfg.trail)) or (en > 0 and v <= en * (1 - cfg.stop_loss)):
            out.add(t)
    return out


def dynamic_plan(holdings: dict[str, float], picks: dict[str, float], exits: set[str],
                 cfg: Config) -> list[tuple[str, str, float]]:
    """Stops-aware rebalance: cut stopped names, trim only above the rug ceiling, let the rest
    ride, and deploy free USDT (above the stable floor) into fresh picks — never trimming a
    winner back to its entry weight. Everything routes through USDT; sells precede buys.
    """
    eq = equity(holdings)
    if eq <= 0:
        return []
    risk_held = {t: v for t, v in holdings.items() if t in HIGHVOL and v >= cfg.min_swap}
    sells: list[tuple[str, str, float]] = []
    kept = dict(risk_held)
    for t in sorted(risk_held):
        v = risk_held[t]
        if t in exits:
            sells.append((t, SETTLEMENT, round(v, 6)))
            kept.pop(t)
        elif v > cfg.hard_cap * eq:                       # trim a runaway back to the entry cap
            cut = v - cfg.max_token * eq
            if cut > cfg.min_swap:
                sells.append((t, SETTLEMENT, round(cut, 6)))
                kept[t] = cfg.max_token * eq
    risk_on_value = sum(kept.values())
    free_usdt = holdings.get(SETTLEMENT, 0.0) + sum(u for _, _, u in sells)
    budget = min(free_usdt - cfg.stable_floor * eq, cfg.aggression * eq - risk_on_value)
    buys: list[tuple[str, str, float]] = []
    fresh = [t for t in picks if t not in kept and t not in exits]  # don't re-enter a name we just stopped
    slots = cfg.n_vehicles - len(kept)
    if budget > cfg.min_swap and slots > 0 and fresh:
        per = min(cfg.max_token * eq, budget / min(slots, len(fresh)))
        for t in sorted(fresh, key=lambda t: (-picks[t], t))[:slots]:
            if per > cfg.min_swap:
                buys.append((SETTLEMENT, t, round(per, 6)))
    return sells + buys


def project_weights(holdings: dict[str, float], plan: list[tuple[str, str, float]]) -> dict[str, float]:
    """Resulting weights if `plan` fills — used to assert the post-trade book stays under the DQ line."""
    eq = equity(holdings)
    if eq <= 0:
        return {SETTLEMENT: 1.0}
    proj = dict(holdings)
    for src, dst, usd in plan:
        proj[src] = proj.get(src, 0.0) - usd
        proj[dst] = proj.get(dst, 0.0) + usd
    return {t: v / eq for t, v in proj.items() if v > 1e-9}
