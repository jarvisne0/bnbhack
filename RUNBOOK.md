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

## 3. Arm the live loop (cadence = every 4h, ≥1 trade/day guaranteed)
The driver is `python -m agent --live` (executes real swaps; refuses unless funded + password).
Schedule it with a systemd timer (installed by sofia/root, runs As=hackagent) so it survives
reboots and logs to the journal. The wallet password must be reachable by the unit (keychain
or `TWAK_WALLET_PASSWORD` in the unit's environment file, 0600).

```
# /etc/systemd/system/bnbagent.service  (User=hackagent, EnvironmentFile with TWAK_WALLET_PASSWORD)
ExecStart=/usr/bin/python3 -m agent --live   (WorkingDirectory=/home/hackagent/bnbagent)
# /etc/systemd/system/bnbagent.timer        OnCalendar=*-*-* 00/4:00:00 ; Persistent=true
```
First live run should be watched: `journalctl -u bnbagent -f`. Confirm the first swap executes
and `state.json` updates HWM.

## 4. Monitor (during Jun 22–28)
- Drawdown vs the 25% breaker / 30% DQ: each run logs `equity`, `hwm`, `drawdown`, `fear_greed`.
- The breaker auto-rotates to 100% USDT at ≥25% DD and holds through a 12h cooldown.
- Out-of-gas guard: alert if BNB on the wallet runs low (mirrors the juzz gas-monitoring rule).

## Safety invariants (enforced in code, unit-tested)
- 25% DD breaker (5% under the 30% DQ) · 25%/token cap (single rug ≤25% < 30%) ·
  20% USDT floor · two-way sellability gate · 2% slippage cap (4% thin memes) · $5 dust floor.
- `agent.run_once` hard-asserts the target's worst-case single-rug loss < 30% before any swap.
