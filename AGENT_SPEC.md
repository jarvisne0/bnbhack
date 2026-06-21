# BNB Hack Track-1 Agent — Spec (lean, deterministic)

Goal: maximize 1-week total return on BSC, **never breach 30% drawdown (= DQ)**, ≥1
trade/day, sim costs. Market is efficient (8 edges tested, all ~0) ⇒ we win as a
**risk-disciplined convex spot bet**, not a signal. Edge = the risk engine + vehicle
selection. Execution = TWAK spot only (verified: swap/transfer/sign-message; NO perps).

## Stack roles (all load-bearing — judged)
- **CMC** = data/selection: sentiment, trending, funding-regime → pick heating vehicles.
- **TWAK** = self-custody execution: `swap --chain bsc`, `wallet portfolio`, `risk`, `compete`.
- **BNB SDK / ERC-8004** = on-chain agent identity + registration.

## Architecture (one deterministic control loop)
```
loop every CADENCE_H hours:
  state   = twak wallet portfolio --json        # USD value per token → equity, HWM, DD
  if DD(equity, HWM) >= DD_STOP:  -> rotate ALL to USDT, enter COOLDOWN  # survive DQ
  else:
    cands = select()                            # CMC sentiment/trending ∩ eligible ∩ risk-ok
    target = size(cands)                         # convex, concentration-capped
    rebalance(target) via twak swap --usd --chain bsc --slippage SLIP
  log(decision, txs)                             # auditable strategy explanation
```

## Risk invariants (HARD — $24k, no mistakes)
1. **DD breaker:** track equity HWM; if drawdown ≥ `DD_STOP=0.25` → swap everything to
   USDT, COOLDOWN `COOLDOWN_H`. 5% margin under the 30% DQ for slippage/latency. NON-NEGOTIABLE.
2. **Per-token concentration cap `MAX_TOKEN=0.25`:** a meme can rug −90–100% in one block
   (no breaker can react). At ≤25%/token a full rug = ≤25% portfolio hit → still under 30%.
   ⇒ convexity is SPREAD across `N_VEHICLES≈3–4` high-vol names, never all-in one.
3. **Slippage cap `SLIP`** per swap (default 2%; per-token override for thin memes); abort if quote slippage > cap.
4. **Pre-trade risk filter:** `twak risk <token>` must pass (no honeypot/scam) before buying.
5. **Dust floor:** skip swaps < `$MIN_SWAP` (avoid churn/fees); skip if quote unfavorable.
6. **Min-trade compliance:** CADENCE guarantees ≥1 trade/day (comp needs ≥7/week).
7. **Stablecoin safe-leg:** baseline allocation `STABLE_FLOOR` always in USDT (dry powder + DD buffer).

## Config (tune with user — NOT assumed)
`CAPITAL` (read live from wallet), `DD_STOP=0.25`, `MAX_TOKEN=0.25`, `N_VEHICLES=4`,
`STABLE_FLOOR=0.20`, `SLIP=0.02`, `CADENCE_H=4`, `COOLDOWN_H=12`, `MIN_SWAP=$5`,
`AGGRESSION` (target risk-on %). **Open decisions for user:** capital amount, DD_STOP,
AGGRESSION/concentration, cadence.

## Universe (from data/)
STABLES = {USDT,USDC,…}; MAJORS = {BNB,ETH,…ballast}; HIGHVOL = tradeable memes
(SKYAI,BANANAS31,TAG,SIREN,MYX,DEXE — vol>$500k/24h, see data/meme_pools.csv). Only
trade eligible∩swappable∩risk-ok.

## Selection (heuristic, honest — not backtested alpha)
score = z(CMC sentiment/trending heat) + z(short-horizon momentum) ; pick top `N_VEHICLES`
of HIGHVOL passing risk filter. Rationale: efficiency proven ⇒ this is vehicle *selection*
for convexity, not a predictive edge. Logged as such.

## Tests (MUST pass before mainnet)
- **unit:** DD-breaker triggers at ≥25%; concentration cap never exceeded; sizer weights
  sum≤1 & respect STABLE_FLOOR; rug-sim (one position→0) keeps portfolio DD<30%; cooldown logic.
- **integration:** full loop in **dry-run** via `twak swap --quote-only` (zero real tx,
  deterministic) over a synthetic price path incl. a −30% crash (breaker must fire) and a rug.
- **testnet** (if available) end-to-end before mainnet arm.

## Deploy
Runs as `hackagent` (contained; no bws keys). `twak compete register` → `0x212c…` before
Jun 22. Pinned deps, fixed seeds → deterministic. Public repo + demo for submission.

## Resolved verifications (as-built 2026-06-18)
- **twak quotes** work on `--chain bsc` for all 6 memes (buy+sell round-trip); priceImpact=slippage proxy.
- **Risk filter:** `twak risk` = 403 (cred tier lacks security scope) → SOFT check only. HARD honeypot/
  liquidity gate = two-way `--quote-only` round-trip (a token must quote a SELL back to USDT). Stronger:
  proves on-chain sellability, not a backend opinion.
- **CMC data path:** Pro REST (Basic plan, 15k credits/mo) — `quotes/latest` (per-token `volume_change_24h`
  = trending) + `fear-and-greed/latest` (market regime, logged). `trending/*` is 403 (plan-gated); the
  Skill-Hub evidence packs are qualitative → kept OUT of the deterministic loop (used for the submission
  writeup / partner award). Key provisioned to hackagent at `~/.config/bnbagent/cmc_key` (0600).
- **Selection is genuinely 3-stack:** CMC `trending` reshuffled picks vs momentum-only (DEXE→top on +62%
  vol; SKYAI dropped on −39% vol) — CMC + twak + BSC all load-bearing.

## Still blocked on the user (single action)
- **FUND** `0x661Cd0cF6F9b1d57845EaC9E82E71Ea11356edD9` (BSC): USDT capital (amount TBD) + ~$5–10 BNB gas.
  Gates BOTH `twak compete register` (deadline **2026-06-25 00:00 UTC**) and live arming. See RUNBOOK.md.
- Organizer ruling (TG): do perps count toward scored portfolio? (parallel; doesn't block spot.)
