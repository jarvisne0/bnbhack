"""CLI entry. Dry-run by default (quote-only, no tx, no funds needed):

    python -m agent                      # real wallet, quote-only
    python -m agent --sim-equity 1000    # inject $1000 USDT to exercise the full loop pre-funding
    python -m agent --live --password ...# ARM: execute real swaps (requires funded wallet)

CMC signals are read from --cmc-json (a {token:{sentiment,trending}} file) when supplied;
absent that, selection runs on twak momentum alone (graceful degradation).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace

from .agent import run_once
from .cmc import Cmc
from .config import Config, SETTLEMENT, load_contracts
from .risk import regime_aggression
from .twakcli import Twak


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent")
    p.add_argument("--live", action="store_true", help="execute real swaps (default: quote-only)")
    p.add_argument("--password", help="wallet password for --live (else env/keychain)")
    p.add_argument("--sim-equity", type=float, help="inject N USDT holdings (dry-run only)")
    p.add_argument("--no-cmc", action="store_true", help="skip CMC; rank on twak momentum alone")
    p.add_argument("--chain", default="bsc")
    args = p.parse_args(argv)

    if args.live and args.sim_equity:
        p.error("--sim-equity is dry-run only; refusing to fake holdings while live")

    tw = Twak(chain=args.chain, quote_only=not args.live)
    if args.sim_equity:
        tw.holdings = lambda: {SETTLEMENT: args.sim_equity}  # type: ignore[method-assign]

    cmc_sig, regime = None, None
    if not args.no_cmc:
        try:
            c = Cmc()
            cmc_sig = c.heat()
            regime = c.fear_greed()  # logged context, not in the deterministic path
        except Exception as e:  # CMC down -> degrade to momentum-only, never block trading
            print(f"# CMC unavailable ({e}); selecting on momentum alone", file=sys.stderr)

    # Spot-only long book can't short a bear: scale the deploy budget down in fear, up to the
    # configured ceiling only in greed. No regime signal -> neutral stance.
    cfg = Config()
    if regime:
        cfg = replace(cfg, aggression=regime_aggression(regime["classification"], cfg.aggression))

    result = run_once(tw, cfg, contracts=load_contracts(), cmc=cmc_sig,
                      password=args.password)
    result["aggression"] = cfg.aggression
    if regime:
        result["fear_greed"] = regime
    print(json.dumps(result, indent=2))
    return 0 if result.get("action") != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
