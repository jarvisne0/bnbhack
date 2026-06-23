"""Empirically pin the stop widths and rebalance cadence from real hourly paths.

Replays the agent's per-position exit logic (trailing stop from peak + hard stop-loss from
entry), but — crucially — only *checks* the stop every `cadence` hours, exactly as the live
2h cron would. For every rolling entry on every token it records the realized exit, then
aggregates the two competing costs:

  * slippage past stop  — how far below the trigger we actually exit because we only look
    every `cadence` hours (latency cost; favours a FAST cadence)
  * whipsaw churn       — fraction of stop-outs where holding to the horizon would have left
    us better off, i.e. we sold a dip that recovered (favours a SLOW cadence)

A 0.15% round-trip is charged on every exit so churn has its real price. No live data, no
network — pure replay of data/prices_1h.parquet. Honest: only 2 of our 6 memes (BANANAS31,
DEXE) are in this set, so we also report the high-volatility subset as the closest proxy for
the actual meme universe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZON_H = 168          # max hold = competition length (~7 days)
ENTRY_STRIDE_H = 12      # start a fresh trial every 12h per token
ROUND_TRIP = 0.0015      # 0.077%/leg, ~0.15% in+out, charged per stop-out
PRICES = "data/prices_1h.parquet"


def simulate(prices: pd.DataFrame, trail: float, stop_loss: float, cadence: int) -> dict:
    """Replay every rolling entry under (trail, stop_loss, cadence); return aggregate costs."""
    exit_rets, slip_past, whipsaw, stopped, held_to_end = [], [], [], 0, 0
    n = len(prices)
    for col in prices.columns:
        p = prices[col].to_numpy(dtype=float)
        for t0 in range(0, n - 1, ENTRY_STRIDE_H):
            entry = p[t0]
            if not np.isfinite(entry) or entry <= 0:
                continue
            end = min(t0 + HORIZON_H, n - 1)
            peak = entry
            exit_i = end
            triggered = False
            # observe only at cadence checkpoints, like the cron
            for t in range(t0 + cadence, end + 1, cadence):
                window_peak = np.nanmax(p[t0 : t + 1])   # peak seen as of this check
                if np.isfinite(window_peak):
                    peak = max(peak, window_peak)
                price = p[t]
                if not np.isfinite(price):
                    continue
                trail_lvl = peak * (1 - trail)
                stop_lvl = entry * (1 - stop_loss)
                if price <= trail_lvl or price <= stop_lvl:
                    exit_i = t
                    triggered = True
                    trigger_lvl = max(trail_lvl, stop_lvl)  # the level we *should* have exited at
                    slip_past.append(max(0.0, (trigger_lvl - price) / entry))
                    break
            ex = p[exit_i]
            if not np.isfinite(ex):
                continue
            ret = ex / entry - 1
            if triggered:
                ret -= ROUND_TRIP
                stopped += 1
                final = p[end]
                if np.isfinite(final) and final > ex:        # would holding have been better?
                    whipsaw.append((final - ex) / entry)
            else:
                held_to_end += 1
            exit_rets.append(ret)
    n_trials = stopped + held_to_end
    return {
        "trail": trail, "stop_loss": stop_loss, "cadence": cadence,
        "n_trials": n_trials,
        "stop_rate": stopped / n_trials if n_trials else 0.0,
        "mean_exit_ret": float(np.mean(exit_rets)) if exit_rets else 0.0,
        "median_exit_ret": float(np.median(exit_rets)) if exit_rets else 0.0,
        "mean_slip_past": float(np.mean(slip_past)) if slip_past else 0.0,
        "p90_slip_past": float(np.percentile(slip_past, 90)) if slip_past else 0.0,
        "whipsaw_rate": len(whipsaw) / stopped if stopped else 0.0,
        "mean_whipsaw_give_up": float(np.mean(whipsaw)) if whipsaw else 0.0,
    }


def main() -> None:
    prices = pd.read_parquet(PRICES)
    # high-vol subset = closest proxy for our actual meme vehicles
    vol = prices.pct_change().std().sort_values(ascending=False)
    hv = list(vol.head(19).index)  # top quartile of 76

    sets = {"all_76": prices, "high_vol_19": prices[hv]}
    cadences = [1, 2, 4, 6]
    widths = [(0.15, 0.12), (0.20, 0.15), (0.10, 0.10)]

    for name, df in sets.items():
        print(f"\n===== {name} ({df.shape[1]} tokens) =====")
        print("CADENCE SWEEP  (trail=0.15, stop_loss=0.12)")
        print(f"{'cad':>4} {'stop%':>7} {'whipsaw%':>9} {'slip_past_mean':>15} "
              f"{'slip_past_p90':>14} {'mean_exit':>10} {'give_up':>9}")
        for cad in cadences:
            r = simulate(df, 0.15, 0.12, cad)
            print(f"{r['cadence']:>4} {r['stop_rate']*100:>6.1f}% {r['whipsaw_rate']*100:>8.1f}% "
                  f"{r['mean_slip_past']*100:>14.3f}% {r['p90_slip_past']*100:>13.3f}% "
                  f"{r['mean_exit_ret']*100:>9.2f}% {r['mean_whipsaw_give_up']*100:>8.2f}%")
        print("STOP-WIDTH SWEEP  (cadence=2)")
        print(f"{'trail':>6} {'stop':>6} {'stop%':>7} {'whipsaw%':>9} {'mean_exit':>10} {'give_up':>9}")
        for tr, sl in widths:
            r = simulate(df, tr, sl, 2)
            print(f"{tr:>6.2f} {sl:>6.2f} {r['stop_rate']*100:>6.1f}% {r['whipsaw_rate']*100:>8.1f}% "
                  f"{r['mean_exit_ret']*100:>9.2f}% {r['mean_whipsaw_give_up']*100:>8.2f}%")


if __name__ == "__main__":
    main()
