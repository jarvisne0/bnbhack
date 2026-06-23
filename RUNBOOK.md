# BNB Hack Track-1 — Arm Runbook

Everything below runs **as `hackagent`** (contained user; holds the wallet + CMC key, no
sofia secrets). Dev/test needs no funds; live arming has one prerequisite: **funding**.

## 0. Status check (anytime, no funds)
```bash
sudo -n -u hackagent bash -lc 'cd ~/bnbagent && python3 -m unittest agent.test_agent'   # 27 tests
sudo -n -u hackagent bash -lc 'twak compete status --json'                              # registered? deadline?
sudo -n -u hackagent bash -lc 'cd ~/bnbagent && python3 -m agent --sim-equity 1000'     # full dry-run (quote-only)
```

## 1. FUND (the only blocker) — user action
Send to the BSC wallet `0x661Cd0cF6F9b1d57845EaC9E82E71Ea11356edD9`:
- **USDT (BEP-20)** — trading capital (amount = user's risk call).
- **~$5–10 BNB** — gas for register + swaps over the week.

Verify it landed:
```bash
sudo -n -u hackagent bash -lc 'twak wallet balance --chain bsc --json'   # expect totalUsd > 0 + USDT in tokens[]
```

## 2. Register on-chain (before 2026-06-25 00:00 UTC; ideally before Jun 22 trading start)
```bash
sudo -n -u hackagent bash -lc 'twak compete register --json'             # uses keychain/TWAK_WALLET_PASSWORD
sudo -n -u hackagent bash -lc 'twak compete status --json'               # confirm registered:true
```

## 3. Arm the live loop (Rust engine; trade every 2h, monitor every 1h)
The live driver is the Rust binary `rust/target/release/bnbagent` (the Python `agent/` is the
parity oracle, not the live path). `scripts/run_agent.sh` is the cron wrapper: it loads the CMC
key and wallet password from 0600 files (never argv) and runs one pass. No-fallback — in `trade`
mode a missing wallet password is a hard stop, not a silent dry-run.

```bash
(cd rust && cargo build --release)                       # build once
install -m700 -d ~/.config/bnbagent
printf '%s' '<WALLET_PASSWORD>' > ~/.config/bnbagent/wallet_pw && chmod 600 ~/.config/bnbagent/wallet_pw
scripts/run_agent.sh dry                                 # final quote-only check before arming
```

Cron (trade on the 2h cadence; equity/drawdown monitor every hour for the DQ trail):
```cron
0 */2 * * *  cd /path/to/bnbhack && scripts/run_agent.sh trade >> logs/agent.log 2>&1
0 *   * * *  cd /path/to/bnbhack && scripts/run_agent.sh log   >> logs/monitor.log 2>&1
```
Watch the first live pass; confirm a swap executes, `rust/state.json` updates HWM, and a `normal`
row lands in `rust/logs/equity.csv`.

## 4. Monitor (during Jun 22–28)
- `rust/logs/equity.csv` (`ts_utc,unix,equity,hwm,drawdown,action`) is the continuous DQ trail —
  appended by both the 2h trade pass and the hourly `log` pass.
- The monitor prints `# WARN drawdown …%` to stderr once DD ≥ 20% (within 5pts of the 25% breaker).
- The breaker auto-rotates to 100% USDT at ≥25% DD and holds through a 12h cooldown.
- Out-of-gas guard: alert if BNB on the wallet runs low (mirrors the juzz gas-monitoring rule).

## Safety invariants (enforced in code, unit-tested)
- 25% DD breaker (5% under the 30% DQ) · 25%/token cap (single rug ≤25% < 30%) ·
  20% USDT floor · two-way sellability gate · 2% slippage cap (4% thin memes) · $5 dust floor.
- `agent.run_once` hard-asserts the target's worst-case single-rug loss < 30% before any swap.
