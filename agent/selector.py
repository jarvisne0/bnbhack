"""Vehicle selection — an honest live heuristic, NOT a backtested alpha.

Price-TA had zero IC (proven), so this does not claim to predict returns. It ranks the
high-variance eligible universe by how much each name is *heating up* (CMC sentiment +
trending) and recent momentum, and picks the top N to spread convexity across. Pure given
its inputs so the choice is reproducible and logged.
"""
from __future__ import annotations

from .config import Config


def _z(values: dict[str, float]) -> dict[str, float]:
    """Z-score a metric across the candidate set (0 if degenerate)."""
    if len(values) < 2:
        return {k: 0.0 for k in values}
    xs = list(values.values())
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    sd = var ** 0.5
    if sd == 0:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / sd for k, v in values.items()}


def score(signals: dict[str, dict[str, float]]) -> dict[str, float]:
    """Combine per-token signals into a convex score.

    signals[token] = {"sentiment": .., "trending": .., "momentum": ..} (any subset).
    Score = mean of the available z-scored metrics. Higher = hotter.
    """
    metrics = ("sentiment", "trending", "momentum")
    zs = {m: _z({t: s[m] for t, s in signals.items() if m in s}) for m in metrics}
    out = {}
    for t in signals:
        comp = [zs[m][t] for m in metrics if t in zs[m]]
        out[t] = sum(comp) / len(comp) if comp else 0.0
    return out


def select(signals: dict[str, dict[str, float]], risk_ok: dict[str, bool],
           cfg: Config) -> dict[str, float]:
    """Pick the top n_vehicles risk-passing names, returning {token: positive score}.

    Scores are shifted to be strictly positive so the sizer treats them as convex weights;
    anything failing the twak risk filter (honeypot/scam) is excluded outright.
    """
    scored = score({t: s for t, s in signals.items() if risk_ok.get(t, False)})
    if not scored:
        return {}
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:cfg.n_vehicles]
    floor = min(v for _, v in ranked)
    # shift so the lowest pick gets a small positive weight, the top pick the largest
    return {t: (v - floor) + 1.0 for t, v in ranked}
