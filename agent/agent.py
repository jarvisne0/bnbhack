"""Deterministic control loop for the BNB Hack Track-1 agent.

One pass per cadence: read portfolio -> update high-water mark -> if drawdown hits the
breaker (or we're cooling down) rotate everything to USDT, else select convex vehicles and
rebalance toward concentration-capped targets. Every swap quote is slippage-checked before
execution; nothing executes while the dry-run guard is on. State (HWM, cooldown) persists
across runs so the breaker survives restarts.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Config, HIGHVOL, SETTLEMENT, load_contracts
from .risk import (breaker_tripped, drawdown, equity, rebalance_plan,
                   target_weights, worst_case_drawdown)
from .selector import select
from .signals import gather
from .twakcli import Twak, TwakError

STATE_PATH = Path(__file__).resolve().parent / "state.json"


@dataclass
class State:
    hwm: float = 0.0
    cooldown_until: float = 0.0
    last_trade_ts: float = 0.0

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "State":
        if path.exists():
            return cls(**json.loads(path.read_text()))
        return cls()

    def save(self, path: Path = STATE_PATH):
        path.write_text(json.dumps(asdict(self), indent=2))


def resolve(token: str, contracts: dict[str, str]) -> str:
    """Symbol -> what twak swap needs: USDT/USDC/BNB by symbol, others by BSC contract."""
    return contracts.get(token, token)


def decide_target(tw: Twak, cfg: Config, contracts: dict[str, str],
                  cmc: dict[str, dict] | None) -> tuple[dict[str, float], str]:
    """Select vehicles passing the sellability gate and size them convexly."""
    cands = {t: contracts[t] for t in HIGHVOL if t in contracts}
    risk_ok = {}
    for t, c in cands.items():
        sellable = tw.sellable(c, slippage=cfg.slip_for(t))
        soft = tw.risk_clean(f"{tw.chain}:{c}")  # None when API unavailable -> ignore
        risk_ok[t] = sellable and (soft is not False)
    sigs = gather(tw, cands, cmc)
    picks = select(sigs, risk_ok, cfg)
    if not picks:
        return {SETTLEMENT: 1.0}, "no vehicle passed risk/selection -> all USDT"
    return target_weights(picks, cfg), f"rebalance into {sorted(picks)}"


def run_once(tw: Twak, cfg: Config, *, contracts: dict[str, str] | None = None,
             cmc: dict[str, dict] | None = None, now: float | None = None,
             password: str | None = None, state_path: Path = STATE_PATH) -> dict:
    contracts = contracts if contracts is not None else load_contracts()
    now = now if now is not None else time.time()
    st = State.load(state_path)

    holdings = tw.holdings()
    eq = equity(holdings)
    if eq < 1.0:  # comp scores any hour starting <=$1 as 0% — surface loudly
        return {"action": "idle", "reason": f"portfolio ${eq:.2f} < $1 — FUND WALLET",
                "equity": eq, "swaps": []}

    st.hwm = max(st.hwm, eq)
    dd = drawdown(eq, st.hwm)

    if breaker_tripped(eq, st.hwm, cfg):
        target, reason = {SETTLEMENT: 1.0}, f"DD BREAKER {dd:.1%} >= {cfg.dd_stop:.0%}"
        st.cooldown_until = now + cfg.cooldown_h * 3600
    elif now < st.cooldown_until:
        target, reason = {SETTLEMENT: 1.0}, f"cooldown ({(st.cooldown_until - now) / 3600:.1f}h left)"
    else:
        target, reason = decide_target(tw, cfg, contracts, cmc)

    assert worst_case_drawdown(target) < 0.30, "target violates the 30% single-rug guard"

    plan = rebalance_plan(holdings, target, cfg)
    swaps = []
    for src, dst, usd in plan:
        risk_tok = dst if src == SETTLEMENT else src
        slip = cfg.slip_for(risk_tok)
        s, d = resolve(src, contracts), resolve(dst, contracts)
        try:
            q = tw.quote(s, d, usd, slip)
        except TwakError as e:
            swaps.append({"src": src, "dst": dst, "usd": usd, "status": f"quote-failed: {e}"})
            continue
        impact = float(q.get("priceImpact", 0) or 0) / 100.0
        if impact > slip:
            swaps.append({"src": src, "dst": dst, "usd": usd,
                          "status": f"slippage {impact:.2%} > cap {slip:.2%} — skipped"})
            continue
        if tw.quote_only:
            swaps.append({"src": src, "dst": dst, "usd": usd, "status": "quoted",
                          "out": q.get("output"), "minReceived": q.get("minReceived")})
        else:
            ex = tw.swap(s, d, usd, slip, password=password)
            st.last_trade_ts = now
            swaps.append({"src": src, "dst": dst, "usd": usd, "status": "executed", "tx": ex})

    st.save(state_path)
    return {"action": "rebalance", "reason": reason, "equity": round(eq, 2),
            "hwm": round(st.hwm, 2), "drawdown": round(dd, 4),
            "target": {k: round(v, 4) for k, v in target.items()}, "swaps": swaps}
