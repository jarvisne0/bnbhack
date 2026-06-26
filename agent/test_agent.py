"""Unit tests for the risk-critical pure core + a mocked dry-run of the loop.

These guard the only thing standing between us and a DQ: the 30% line. Run:
    python -m unittest agent.test_agent -v
"""
import tempfile
import unittest
from pathlib import Path

from .config import Config, SETTLEMENT
from .risk import (breaker_tripped, drawdown, dynamic_plan, equity, heartbeat_plan,
                   project_weights, rebalance_plan, regime_aggression, stop_exits,
                   target_weights, track_positions, worst_case_drawdown)
from .selector import score, select
from . import agent as agentmod

CFG = Config()


class TestDrawdown(unittest.TestCase):
    def test_at_peak_zero(self):
        self.assertEqual(drawdown(100, 100), 0.0)

    def test_above_peak_clamped(self):
        self.assertEqual(drawdown(120, 100), 0.0)

    def test_fractional(self):
        self.assertAlmostEqual(drawdown(75, 100), 0.25)

    def test_zero_hwm(self):
        self.assertEqual(drawdown(0, 0), 0.0)

    def test_breaker_boundary(self):
        # exactly at dd_stop trips; just under does not
        self.assertTrue(breaker_tripped(75.0, 100.0, CFG))            # 25% == dd_stop
        self.assertFalse(breaker_tripped(75.01, 100.0, CFG))
        self.assertTrue(breaker_tripped(60.0, 100.0, CFG))            # 40% > dd_stop

    def test_regime_aggression_scales_by_fear(self):
        self.assertEqual(regime_aggression("Extreme fear", 0.60), 0.30)  # contrarian: oversold dip = deploy
        self.assertEqual(regime_aggression("Fear", 0.60), 0.35)
        self.assertEqual(regime_aggression("Neutral", 0.60), 0.50)
        self.assertEqual(regime_aggression("Greed", 0.60), 0.60)
        self.assertEqual(regime_aggression("Extreme greed", 0.60), 0.60)
        self.assertEqual(regime_aggression("", 0.60), 0.50)          # unknown -> neutral
        self.assertEqual(regime_aggression("neutral", 0.30), 0.30)   # capped by ceiling


class TestTargetWeights(unittest.TestCase):
    def test_sums_to_one(self):
        w = target_weights({"SKYAI": 2, "TAG": 1, "SIREN": 1}, CFG)
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)

    def test_concentration_cap_never_exceeded(self):
        # a single dominant score must still be clamped to max_token
        w = target_weights({"SKYAI": 1000, "TAG": 1}, CFG)
        for t, x in w.items():
            if t != SETTLEMENT:
                self.assertLessEqual(x, CFG.max_token + 1e-9, f"{t}={x} exceeds cap")

    def test_stable_floor_respected(self):
        w = target_weights({"SKYAI": 1, "TAG": 1, "SIREN": 1, "MYX": 1}, CFG)
        self.assertGreaterEqual(w.get(SETTLEMENT, 0.0), CFG.stable_floor - 1e-9)

    def test_no_candidates_all_stable(self):
        w = target_weights({}, CFG)
        self.assertEqual(w, {SETTLEMENT: 1.0})

    def test_negative_scores_ignored(self):
        w = target_weights({"SKYAI": -5, "TAG": -1}, CFG)
        self.assertEqual(w, {SETTLEMENT: 1.0})

    def test_aggression_caps_risk_on(self):
        w = target_weights({"SKYAI": 1, "TAG": 1}, CFG)
        risk_on = sum(v for t, v in w.items() if t != SETTLEMENT)
        self.assertLessEqual(risk_on, CFG.aggression + 1e-9)

    def test_worst_case_under_dq(self):
        # property: across many score shapes, no target can lose >=30% to one rug
        for scores in [{"A": 9, "B": 1}, {"A": 1, "B": 1, "C": 1, "D": 1},
                       {"A": 100}, {"A": 3, "B": 2, "C": 1}]:
            w = target_weights(scores, CFG)
            self.assertLess(worst_case_drawdown(w), 0.30)


class TestRugSurvival(unittest.TestCase):
    def test_single_rug_keeps_dd_under_30(self):
        # max concentration, then the biggest position goes to zero in one block
        w = target_weights({"SKYAI": 1, "TAG": 1, "SIREN": 1, "MYX": 1}, CFG)
        eq0 = 1000.0
        holdings = {t: x * eq0 for t, x in w.items()}
        biggest = max((t for t in holdings if t != SETTLEMENT), key=lambda t: holdings[t])
        holdings[biggest] = 0.0  # rug
        new_eq = equity(holdings)
        self.assertLess(drawdown(new_eq, eq0), 0.30, "a single rug breached the DQ line")

    def test_two_independent_rugs_still_recoverable(self):
        # even two simultaneous rugs at the cap stay within a survivable band
        w = target_weights({"A": 1, "B": 1, "C": 1, "D": 1}, CFG)
        eq0 = 1000.0
        holdings = {t: x * eq0 for t, x in w.items()}
        risk = [t for t in holdings if t != SETTLEMENT]
        for t in sorted(risk, key=lambda t: -holdings[t])[:2]:
            holdings[t] = 0.0
        # two 25%-cap rugs is at most the risk-on budget; never a total wipe
        self.assertLess(drawdown(equity(holdings), eq0), CFG.aggression + 1e-9)


class TestRebalancePlan(unittest.TestCase):
    def test_empty_when_no_equity(self):
        self.assertEqual(rebalance_plan({}, {"SKYAI": 1.0}, CFG), [])

    def test_sells_before_buys(self):
        holdings = {"SKYAI": 500, SETTLEMENT: 500}
        target = {"TAG": 0.5, SETTLEMENT: 0.5}
        plan = rebalance_plan(holdings, target, CFG)
        kinds = [src == SETTLEMENT for src, _, _ in plan]  # False(sell) before True(buy)
        self.assertEqual(kinds, sorted(kinds))
        self.assertTrue(all(s == SETTLEMENT or d == SETTLEMENT for s, d, _ in plan))

    def test_skips_dust(self):
        holdings = {"SKYAI": 100.0, SETTLEMENT: 100.0}
        target = {"SKYAI": 0.5 + 0.001, SETTLEMENT: 0.5 - 0.001}  # ~$0.20 delta < min_swap
        self.assertEqual(rebalance_plan(holdings, target, CFG), [])

    def test_full_rotation_to_usdt(self):
        holdings = {"SKYAI": 250, "TAG": 250, SETTLEMENT: 500}
        plan = rebalance_plan(holdings, {SETTLEMENT: 1.0}, CFG)
        self.assertTrue(all(d == SETTLEMENT for _, d, _ in plan))
        self.assertEqual({s for s, _, _ in plan}, {"SKYAI", "TAG"})


class TestStops(unittest.TestCase):
    def test_track_records_entry_and_ratchets_peak(self):
        peaks, entries = track_positions({"SKYAI": 100.0}, {}, {})
        self.assertEqual((peaks, entries), ({"SKYAI": 100.0}, {"SKYAI": 100.0}))
        peaks, entries = track_positions({"SKYAI": 130.0}, peaks, entries)
        self.assertEqual(peaks["SKYAI"], 130.0)        # peak ratchets up
        self.assertEqual(entries["SKYAI"], 100.0)      # entry stays
        peaks, entries = track_positions({"SKYAI": 110.0}, peaks, entries)
        self.assertEqual(peaks["SKYAI"], 130.0)        # peak does not fall back

    def test_track_drops_closed_positions(self):
        peaks, entries = track_positions({}, {"SKYAI": 100.0}, {"SKYAI": 100.0})
        self.assertEqual((peaks, entries), ({}, {}))

    def test_trailing_stop_fires(self):
        exits = stop_exits({"SKYAI": 85.0}, {"SKYAI": 100.0}, {"SKYAI": 100.0}, CFG)
        self.assertEqual(exits, {"SKYAI"})             # 15% off the peak

    def test_stop_loss_fires(self):
        exits = stop_exits({"SKYAI": 87.0}, {"SKYAI": 100.0}, {"SKYAI": 100.0}, CFG)
        self.assertEqual(exits, {"SKYAI"})             # 13% underwater from entry (> 12% stop)

    def test_winner_runs_no_exit(self):
        # up 20%, pulled back only to 110 from a 120 peak: inside both bands -> hold
        exits = stop_exits({"SKYAI": 110.0}, {"SKYAI": 120.0}, {"SKYAI": 100.0}, CFG)
        self.assertEqual(exits, set())

    def test_dynamic_sells_stopped_position(self):
        plan = dynamic_plan({"SKYAI": 250.0, SETTLEMENT: 750.0}, {}, {"SKYAI"}, CFG)
        self.assertIn(("SKYAI", SETTLEMENT, 250.0), plan)

    def test_dynamic_trims_runaway_above_hard_cap(self):
        h = {"SKYAI": 400.0, SETTLEMENT: 600.0}        # 40% of a $1000 book, above the 28% ceiling
        plan = dynamic_plan(h, {}, set(), CFG)
        sells = [p for p in plan if p[0] == "SKYAI"]
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0][2], 130.0)     # trimmed back to max_token (27% = $270)
        self.assertLess(worst_case_drawdown(project_weights(h, plan)), 0.30)

    def test_dynamic_lets_winner_run_below_hard_cap(self):
        # 27% < 28% ceiling: a winner between the entry cap and the ceiling is NOT trimmed
        h = {"SKYAI": 270.0, SETTLEMENT: 730.0}
        plan = dynamic_plan(h, {}, set(), CFG)
        self.assertEqual([p for p in plan if p[0] == "SKYAI"], [])

    def test_dynamic_deploys_into_fresh_picks_capped(self):
        plan = dynamic_plan({SETTLEMENT: 1000.0}, {"TAG": 1.0, "SIREN": 0.8}, set(), CFG)
        self.assertTrue(plan and all(s == SETTLEMENT for s, _, _ in plan))   # all buys
        self.assertTrue(all(usd <= CFG.max_token * 1000 + 1e-6 for _, _, usd in plan))
        self.assertLess(worst_case_drawdown(project_weights({SETTLEMENT: 1000.0}, plan)), 0.30)


class TestSelector(unittest.TestCase):
    def test_excludes_risk_failures(self):
        sigs = {"SKYAI": {"momentum": 0.1}, "TAG": {"momentum": 0.2}}
        picks = select(sigs, {"SKYAI": True, "TAG": False}, CFG)
        self.assertIn("SKYAI", picks)
        self.assertNotIn("TAG", picks)

    def test_picks_are_positive(self):
        sigs = {"A": {"momentum": 0.3}, "B": {"momentum": -0.1}, "C": {"momentum": 0.1}}
        picks = select(sigs, {k: True for k in sigs}, CFG)
        self.assertTrue(all(v > 0 for v in picks.values()))

    def test_caps_at_n_vehicles(self):
        sigs = {c: {"momentum": i * 0.1} for i, c in enumerate("ABCDEFG")}
        picks = select(sigs, {k: True for k in sigs}, CFG)
        self.assertLessEqual(len(picks), CFG.n_vehicles)

    def test_score_handles_missing_metrics(self):
        # mixed availability must not crash and must rank present metrics
        s = score({"A": {"momentum": 0.2, "sentiment": 1.0}, "B": {"momentum": 0.1}})
        self.assertEqual(set(s), {"A", "B"})


class FakeTwak:
    """Deterministic stand-in for the twak CLI — no subprocess, no network."""
    def __init__(self, holdings, quote_only=True, chain="bsc", impact=0.0):
        self._holdings = holdings
        self.quote_only = quote_only
        self.chain = chain
        self.impact = impact

    def holdings(self):
        return dict(self._holdings)

    def sellable(self, contract, usd=25.0, slippage=0.05):
        return True

    def risk_clean(self, asset_id):
        return None  # API unavailable -> ignored, like live

    def price_history(self, token, period="day"):
        return {"priceUsd": 1.1, "history": [{"price": 1.0, "date": 0}]}  # +10% momentum

    def quote(self, src, dst, usd, slippage):
        return {"output": f"{usd} {dst}", "minReceived": f"{usd*0.98} {dst}",
                "priceImpact": str(self.impact * 100)}


class TestLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"
        self.contracts = {t: f"0x{t}" for t in ["SKYAI", "BANANAS31", "TAG", "SIREN", "MYX", "DEXE"]}
        self.cmc = {t: {"sentiment": 1.0, "trending": 1.0} for t in self.contracts}

    def test_idle_when_unfunded(self):
        tw = FakeTwak({SETTLEMENT: 0.50})
        r = agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=0.0, state_path=self.tmp)
        self.assertEqual(r["action"], "idle")
        self.assertIn("FUND", r["reason"])

    def test_breaker_rotates_to_usdt(self):
        # first run sets HWM at $1000
        tw = FakeTwak({SETTLEMENT: 1000.0})
        agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                          now=0.0, state_path=self.tmp)
        # equity drops 30% -> breaker must target 100% USDT
        tw2 = FakeTwak({"SKYAI": 700.0})
        r = agentmod.run_once(tw2, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=100.0, state_path=self.tmp)
        self.assertEqual(r["target"], {SETTLEMENT: 1.0})
        self.assertIn("BREAKER", r["reason"])

    def test_cooldown_holds_usdt(self):
        tw = FakeTwak({SETTLEMENT: 1000.0})
        agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc, now=0.0, state_path=self.tmp)
        tw2 = FakeTwak({"SKYAI": 600.0})  # 40% dd trips breaker, sets cooldown
        agentmod.run_once(tw2, CFG, contracts=self.contracts, cmc=self.cmc, now=100.0, state_path=self.tmp)
        # recovered above the breaker line (20% dd < 25%) but still inside cooldown -> hold USDT
        tw3 = FakeTwak({SETTLEMENT: 800.0})
        r = agentmod.run_once(tw3, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=100.0 + 3600, state_path=self.tmp)
        self.assertEqual(r["target"], {SETTLEMENT: 1.0})
        self.assertIn("cooldown", r["reason"])

    def test_normal_rebalance_quotes_only(self):
        tw = FakeTwak({SETTLEMENT: 1000.0})
        r = agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=0.0, state_path=self.tmp)
        self.assertEqual(r["action"], "rebalance")
        self.assertLess(worst_case_drawdown(r["target"]), 0.30)
        self.assertTrue(all(s["status"] == "quoted" for s in r["swaps"]))

    def test_high_slippage_swap_skipped(self):
        tw = FakeTwak({SETTLEMENT: 1000.0}, impact=0.50)  # 50% impact >> caps
        r = agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=0.0, state_path=self.tmp)
        self.assertTrue(all("slippage" in s["status"] for s in r["swaps"]))


class TestHeartbeat(unittest.TestCase):
    def test_round_trips_largest_holding(self):
        plan = heartbeat_plan({"SKYAI": 3.0, "TAG": 2.0, SETTLEMENT: 5.0}, {}, CFG)
        self.assertEqual(plan, [("SKYAI", SETTLEMENT, 1.0), (SETTLEMENT, "SKYAI", 1.0)])

    def test_deploys_top_pick_when_all_cash(self):
        plan = heartbeat_plan({SETTLEMENT: 100.0}, {"TAG": 1.0, "SIREN": 0.8}, CFG)
        self.assertEqual(plan, [(SETTLEMENT, "TAG", 1.0)])

    def test_holds_when_floor_would_break(self):
        self.assertEqual(heartbeat_plan({SETTLEMENT: 1.0}, {"TAG": 1.0}, CFG), [])


class TestHeartbeatLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "state.json"
        self.contracts = {t: f"0x{t}" for t in ["SKYAI", "BANANAS31", "TAG", "SIREN", "MYX", "DEXE"]}
        self.cmc = {t: {"sentiment": 1.0, "trending": 1.0} for t in self.contracts}

    def test_stale_converged_book_forces_activity_swap(self):
        # slot full + cash at the floor: dynamic_plan is empty, but last trade is ancient -> heartbeat
        tw = FakeTwak({"SKYAI": 270.0, SETTLEMENT: 730.0})
        r = agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=1e6, state_path=self.tmp)
        self.assertIn("heartbeat", r["reason"])
        self.assertTrue(r["swaps"])  # would otherwise be [] on a converged book

    def test_recent_trade_no_heartbeat(self):
        agentmod.State(hwm=1000.0, last_trade_ts=1e6 - 3600).save(self.tmp)  # traded 1h ago
        tw = FakeTwak({"SKYAI": 270.0, SETTLEMENT: 730.0})
        r = agentmod.run_once(tw, CFG, contracts=self.contracts, cmc=self.cmc,
                              now=1e6, state_path=self.tmp)
        self.assertNotIn("heartbeat", r["reason"])
        self.assertEqual(r["swaps"], [])


if __name__ == "__main__":
    unittest.main()
