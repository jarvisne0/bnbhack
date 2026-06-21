# bnbagent — Rust trading engine (deterministic single binary)

The live engine for the competition. A port of the Python agent (`../agent/`) to a single static
Rust binary, for **deterministic failure modes** — the Python core stays in the repo as the parity
oracle, not the live path.

## Why Rust here

The risk core is the only thing between us and a 30% drawdown DQ. Rust makes its failure modes
*defined* rather than incidental:

- **Typed I/O** — every twak/CMC response is parsed into a struct (`serde`); a parse or IO failure
  returns an `Err` the caller maps to a safe action (hold, skip a token, or report `action:"error"`
  and trade nothing) — never an undefined crash mid-trade.
- **Exhaustive state machine** — `enum Decision { Breaker, Cooldown, Normal }` matched exhaustively;
  a case cannot be silently dropped.
- **Deterministic iteration** — `BTreeMap`/`BTreeSet` everywhere, so ordering never depends on hash
  seeds; selection ties break by token name, identically to Python.
- **Bounded panic** — the single `assert!` (post-trade book under the 30% line) is an invariant
  guard whose contract is *halt*, with `panic = "abort"` in release. A safe stop, not a wild one.
- **One binary** — no interpreter, venv, or dependency drift on the box.

`f64` is used throughout (not Decimal) precisely so decisions are **byte-identical** to the Python
engine — the determinism win is the type system and control flow, not the number type.

## Run

```
cargo test                              # 18 unit tests (risk core + selector)
cargo run -- --parity                   # canonical decision dump
cargo run -- --sim-equity 1000 --no-cmc # full loop vs live twak quotes, dry-run, $1000 injected
cargo run --release -- --no-cmc         # real wallet, quote-only
cargo run --release -- --live --password <pw>   # ARM: execute real swaps (funded wallet)
```

Reads `data/token_contracts.json` and a CMC key from `CMC_API_KEY` or `~/.config/bnbagent/cmc_key`.
Persists `state.json` (HWM, cooldown, per-position peaks/entries) between runs. Drive it on a
**2-hour cron** for the competition cadence.

## Parity with Python

`cargo run -- --parity` and `python3 ../scripts/parity.py` emit the same canonical decision dump for
a fixed set of scenarios (select, deploy, trim, stop+redeploy, full rotation). They are
**byte-identical** — the Rust engine cannot silently diverge from the audited Python logic.
