"""Python side of the Rust<->Python parity proof. Runs the identical fixed scenarios as the
Rust `--parity` dump and prints canonical strings, so `diff` of the two outputs proves the two
engines make byte-identical decisions. No I/O, no live data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import Config, SETTLEMENT
from agent.risk import dynamic_plan, heartbeat_plan, project_weights, rebalance_plan
from agent.selector import select

cfg = Config()


def fmt_plan(p):
    return [f"{s}>{d}:{u:.6f}" for s, d, u in p]


def fmt_book(b):
    return [f"{t}:{v:.6f}" for t, v in sorted(b.items())]


sigs = {
    "SKYAI": {"momentum": 0.20, "trending": 50.0},
    "TAG": {"momentum": 0.10, "trending": 80.0},
    "SIREN": {"momentum": -0.05, "trending": 10.0},
    "MYX": {"momentum": 0.30, "trending": -20.0},
    "DEXE": {"momentum": 0.15, "trending": 62.0},
}
risk_ok = {t: True for t in sigs}
picks = select(sigs, risk_ok, cfg)

b_h = {SETTLEMENT: 1000.0}
b = dynamic_plan(b_h, picks, set(), cfg)
c_h = {"SKYAI": 400.0, "TAG": 150.0, SETTLEMENT: 450.0}
c = dynamic_plan(c_h, picks, set(), cfg)
d_h = {"SKYAI": 200.0, "TAG": 200.0, SETTLEMENT: 600.0}
d = dynamic_plan(d_h, picks, {"TAG"}, cfg)
e = rebalance_plan({"SKYAI": 250.0, "TAG": 250.0, SETTLEMENT: 500.0}, {SETTLEMENT: 1.0}, cfg)
f1 = heartbeat_plan({"SKYAI": 300.0, "TAG": 250.0, SETTLEMENT: 450.0}, picks, cfg)
f2 = heartbeat_plan({SETTLEMENT: 1000.0}, picks, cfg)

print(json.dumps({
    "A_select": fmt_book(picks),
    "B_deploy": fmt_plan(b),
    "B_weights": fmt_book(project_weights(b_h, b)),
    "C_trim": fmt_plan(c),
    "C_weights": fmt_book(project_weights(c_h, c)),
    "D_stop_redeploy": fmt_plan(d),
    "E_rotation": fmt_plan(e),
    "F_heartbeat_held": fmt_plan(f1),
    "F_heartbeat_cash": fmt_plan(f2),
}, indent=2, sort_keys=True))
