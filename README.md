# BNB Hack Track 1 — Risk-Disciplined Convex Spot Agent

A live, self-custodial trading agent for BNB Smart Chain. It trades the competition's
eligible CoinMarketCap-listed BEP-20 tokens through the **Trust Wallet Agent Kit (twak)**,
driven by **CoinMarketCap** signal, registered and identified **on-chain (BSC / ERC-8004)**.

## Thesis: in this game, survival is the edge

We tested eight candidate edges on BSC (mean-reversion, funding, cross-pair dislocation,
momentum, and more). After costs they all decayed to roughly zero — the liquid BSC universe
is efficient. So we do **not** pretend to have predictive alpha.

Instead we play the game exactly as it is scored: a one-week total-return contest where
(a) a **30% drawdown disqualifies you**, (b) any hour that begins with a portfolio worth
**≤ $1 scores 0%**, and (c) you must place **≥ 1 trade/day** in the eligible-token set.
In a contest like this most agents self-destruct — a meme rugs, a drawdown trips the DQ, or
the wallet bleeds to dust. Our edge is the **risk engine** that makes those failure modes
structurally hard to hit while keeping convex upside exposure.

## The loop (deterministic, every 4 hours)

1. **Read** the live portfolio via twak → equity, high-water mark, drawdown.
2. **Kill-switch** — if drawdown ≥ **25%** (a 5% margin under the 30% DQ line, for
   slippage/latency), rotate everything to USDT and enter cooldown. Non-negotiable.
3. **Select** 3–4 "heating" high-volatility vehicles, ranked by **CMC** signal
   (24h volume-change / trending heat) + short-horizon momentum, intersected with the
   eligible-token list and a sellability gate.
4. **Size convexly** — a hard **25% per-token cap** and a **20% USDT floor**, so no single
   position can exceed 25%. Even a 100% rug of one vehicle is a ≤25% portfolio hit —
   structurally under the DQ line. The remainder sits in USDT (dry powder + DQ buffer).
5. **Rebalance** via `twak swap --chain bsc`. Every quote is slippage-checked (abort > 2%);
   every buy is gated by a **two-way quote round-trip** — the token must quote a sell back to
   USDT, which proves on-chain sellability (our honeypot filter, stronger than a backend opinion).

State (high-water mark, cooldown) persists across restarts, so the breaker survives a crash.
A dry-run guard (`--quote-only`) runs the whole loop with zero real transactions.

## Why the three required stacks are all load-bearing

- **CoinMarketCap** — data & selection: per-token `volume_change_24h` (trending heat) plus
  the Fear & Greed regime decide *which* vehicles to hold. (CMC `trending` reshuffles picks
  vs momentum-only — e.g. DEXE rises to the top on +62% volume, SKYAI is dropped on −39%.)
- **Trust Wallet Agent Kit** — self-custody execution: portfolio reads, BSC swaps, and the
  on-chain competition registration (`twak compete register`).
- **BSC / ERC-8004** — the chain itself plus the on-chain agent identity & participant registry.

## What we are and aren't claiming

We are **not** claiming to predict the market. We are claiming that a convex bet on volatile,
CMC-trending names — spread so no single rug can disqualify us, with a kill-switch 5% under the
DQ line and capital kept fully deployed so no hour scores zero — captures upside while
structurally surviving the failure modes that knock out undisciplined agents.

The edge is the discipline, so the discipline is the part we tested hardest: **27 unit tests**
covering the breaker firing at 25%, the concentration cap never being breached, the sizer
respecting the stable floor, and a simulated single-position rug keeping portfolio drawdown
under 30%.

```
python3 -m pytest agent/test_agent.py      # 27 passed
python3 -m agent --quote-only              # full loop, zero real tx
```

See [`AGENT_SPEC.md`](AGENT_SPEC.md) for the full spec and [`RUNBOOK.md`](RUNBOOK.md) for
funding, registration, and arming.
