//! Pure risk functions — no I/O, fully deterministic, unit-tested. These encode the only edge
//! we have: discipline. They never panic in normal operation; the one assertion (post-trade book
//! under the 30% line) is an invariant guard whose contract is "halt", a bounded failure mode.

use std::collections::{BTreeMap, BTreeSet};

use crate::config::{Config, SETTLEMENT};

pub type Book = BTreeMap<String, f64>;

#[derive(Clone, Debug, serde::Serialize)]
pub struct Swap {
    pub src: String,
    pub dst: String,
    pub usd: f64,
}

/// The control-loop state, decided once per pass. Exhaustive: a case cannot be forgotten.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Decision {
    Breaker,
    Cooldown,
    Normal,
}

fn round6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

fn swap(src: &str, dst: &str, usd: f64) -> Swap {
    Swap { src: src.to_string(), dst: dst.to_string(), usd: round6(usd) }
}

pub fn equity(holdings: &Book) -> f64 {
    holdings.values().sum()
}

/// Fractional drawdown from the high-water mark (0.0 at/above the peak).
pub fn drawdown(eq: f64, hwm: f64) -> f64 {
    if hwm <= 0.0 {
        return 0.0;
    }
    (1.0 - eq / hwm).max(0.0)
}

pub fn breaker_tripped(eq: f64, hwm: f64, cfg: &Config) -> bool {
    drawdown(eq, hwm) >= cfg.dd_stop
}

/// Largest single-token weight = worst instantaneous loss if one vehicle rugs to zero.
pub fn worst_case_drawdown(target: &Book) -> f64 {
    target
        .iter()
        .filter(|(t, _)| t.as_str() != SETTLEMENT)
        .map(|(_, w)| *w)
        .fold(0.0, f64::max)
}

/// Risk holdings only (non-settlement, positive value).
pub fn risk_held(holdings: &Book) -> Book {
    holdings
        .iter()
        .filter(|(t, v)| t.as_str() != SETTLEMENT && **v > 0.0)
        .map(|(t, v)| (t.clone(), *v))
        .collect()
}

/// Carry per-position entry value and running peak, observed from live holdings.
pub fn track_positions(held: &Book, peaks: &Book, entries: &Book) -> (Book, Book) {
    let new_peaks = held
        .iter()
        .map(|(t, v)| (t.clone(), peaks.get(t).copied().unwrap_or(*v).max(*v)))
        .collect();
    let new_entries = held
        .iter()
        .map(|(t, v)| (t.clone(), entries.get(t).copied().unwrap_or(*v)))
        .collect();
    (new_peaks, new_entries)
}

/// Positions to liquidate now: fell `trail` from peak, or `stop_loss` underwater from entry.
pub fn stop_exits(held: &Book, peaks: &Book, entries: &Book, cfg: &Config) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for (t, v) in held {
        let pk = peaks.get(t).copied().unwrap_or(*v);
        let en = entries.get(t).copied().unwrap_or(*v);
        if (pk > 0.0 && *v <= pk * (1.0 - cfg.trail)) || (en > 0.0 && *v <= en * (1.0 - cfg.stop_loss)) {
            out.insert(t.clone());
        }
    }
    out
}

/// Diff current vs target into a deterministic sell-then-buy plan. Used for the breaker/cooldown
/// full rotation to USDT. Everything routes through USDT; deltas below min_swap are skipped.
pub fn rebalance_plan(holdings: &Book, target: &Book, cfg: &Config) -> Vec<Swap> {
    let eq = equity(holdings);
    if eq <= 0.0 {
        return vec![];
    }
    let mut keys: BTreeSet<&String> = holdings.keys().collect();
    keys.extend(target.keys());
    let (mut sells, mut buys) = (vec![], vec![]);
    for t in keys {
        if t == SETTLEMENT {
            continue;
        }
        let desired = target.get(t).copied().unwrap_or(0.0) * eq;
        let d = desired - holdings.get(t).copied().unwrap_or(0.0);
        if d < -cfg.min_swap {
            sells.push(swap(t, SETTLEMENT, -d));
        } else if d > cfg.min_swap {
            buys.push(swap(SETTLEMENT, t, d));
        }
    }
    sells.extend(buys);
    sells
}

/// Stops-aware rebalance: cut stopped names, trim only above the rug ceiling, let the rest ride,
/// and deploy free USDT (above the stable floor) into fresh picks — never trimming a winner back
/// to its entry weight. Everything routes through USDT; sells precede buys.
pub fn dynamic_plan(holdings: &Book, picks: &Book, exits: &BTreeSet<String>, cfg: &Config) -> Vec<Swap> {
    let eq = equity(holdings);
    if eq <= 0.0 {
        return vec![];
    }
    let held = risk_held(holdings);
    let mut sells = vec![];
    let mut kept = held.clone();
    for (t, v) in &held {
        if exits.contains(t) {
            sells.push(swap(t, SETTLEMENT, *v));
            kept.remove(t);
        } else if *v > cfg.hard_cap * eq {
            let cut = *v - cfg.max_token * eq;
            if cut > cfg.min_swap {
                sells.push(swap(t, SETTLEMENT, cut));
                kept.insert(t.clone(), cfg.max_token * eq);
            }
        }
    }
    let risk_on_value: f64 = kept.values().sum();
    let free_usdt = holdings.get(SETTLEMENT).copied().unwrap_or(0.0)
        + sells.iter().map(|s| s.usd).sum::<f64>();
    let budget = (free_usdt - cfg.stable_floor * eq).min(cfg.aggression * eq - risk_on_value);

    // fresh picks not held and not just stopped, ranked by score desc then token (Python parity)
    let mut fresh: Vec<(&String, f64)> = picks
        .iter()
        .filter(|(t, _)| !kept.contains_key(*t) && !exits.contains(*t))
        .map(|(t, s)| (t, *s))
        .collect();
    fresh.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then(a.0.cmp(b.0)));
    let slots = cfg.n_vehicles.saturating_sub(kept.len());

    let mut buys = vec![];
    if budget > cfg.min_swap && slots > 0 && !fresh.is_empty() {
        let per = (cfg.max_token * eq).min(budget / slots.min(fresh.len()) as f64);
        for (t, _) in fresh.into_iter().take(slots) {
            if per > cfg.min_swap {
                buys.push(swap(SETTLEMENT, t, per));
            }
        }
    }
    sells.extend(buys);
    sells
}

/// Resulting weights if `plan` fills — used to assert the post-trade book stays under the DQ line.
pub fn project_weights(holdings: &Book, plan: &[Swap]) -> Book {
    let eq = equity(holdings);
    if eq <= 0.0 {
        return BTreeMap::from([(SETTLEMENT.to_string(), 1.0)]);
    }
    let mut proj = holdings.clone();
    for s in plan {
        *proj.entry(s.src.clone()).or_insert(0.0) -= s.usd;
        *proj.entry(s.dst.clone()).or_insert(0.0) += s.usd;
    }
    proj.into_iter().filter(|(_, v)| *v > 1e-9).map(|(t, v)| (t, v / eq)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn book(pairs: &[(&str, f64)]) -> Book {
        pairs.iter().map(|(t, v)| (t.to_string(), *v)).collect()
    }
    fn cfg() -> Config {
        Config::default()
    }
    fn has_sell(plan: &[Swap], tok: &str) -> bool {
        plan.iter().any(|s| s.src == tok && s.dst == SETTLEMENT)
    }

    #[test]
    fn drawdown_basics() {
        assert_eq!(drawdown(100.0, 100.0), 0.0);
        assert_eq!(drawdown(120.0, 100.0), 0.0); // above peak clamped
        assert!((drawdown(75.0, 100.0) - 0.25).abs() < 1e-12);
        assert_eq!(drawdown(0.0, 0.0), 0.0);
    }

    #[test]
    fn breaker_boundary() {
        let c = cfg();
        assert!(breaker_tripped(75.0, 100.0, &c)); // 25% == dd_stop trips
        assert!(!breaker_tripped(75.01, 100.0, &c));
        assert!(breaker_tripped(60.0, 100.0, &c));
    }

    #[test]
    fn single_rug_keeps_dd_under_30() {
        // worst case from dynamic deploy: no single position can exceed the entry cap
        let plan = dynamic_plan(&book(&[("USDT", 1000.0)]),
            &book(&[("A", 1.0), ("B", 1.0), ("C", 1.0), ("D", 1.0)]), &BTreeSet::new(), &cfg());
        let w = project_weights(&book(&[("USDT", 1000.0)]), &plan);
        assert!(worst_case_drawdown(&w) < 0.30);
    }

    #[test]
    fn rebalance_empty_no_equity() {
        assert!(rebalance_plan(&Book::new(), &book(&[("A", 1.0)]), &cfg()).is_empty());
    }

    #[test]
    fn rebalance_sells_before_buys_and_routes_usdt() {
        let plan = rebalance_plan(&book(&[("SKYAI", 500.0), ("USDT", 500.0)]),
            &book(&[("TAG", 0.5), ("USDT", 0.5)]), &cfg());
        let kinds: Vec<bool> = plan.iter().map(|s| s.src == SETTLEMENT).collect();
        let mut sorted = kinds.clone();
        sorted.sort();
        assert_eq!(kinds, sorted); // sells(false) before buys(true)
        assert!(plan.iter().all(|s| s.src == SETTLEMENT || s.dst == SETTLEMENT));
    }

    #[test]
    fn rebalance_full_rotation() {
        let plan = rebalance_plan(&book(&[("SKYAI", 250.0), ("TAG", 250.0), ("USDT", 500.0)]),
            &book(&[("USDT", 1.0)]), &cfg());
        assert!(plan.iter().all(|s| s.dst == SETTLEMENT));
        let srcs: BTreeSet<&str> = plan.iter().map(|s| s.src.as_str()).collect();
        assert_eq!(srcs, BTreeSet::from(["SKYAI", "TAG"]));
    }

    #[test]
    fn track_records_and_ratchets() {
        let (p, e) = track_positions(&book(&[("SKYAI", 100.0)]), &Book::new(), &Book::new());
        assert_eq!(p["SKYAI"], 100.0);
        assert_eq!(e["SKYAI"], 100.0);
        let (p, e) = track_positions(&book(&[("SKYAI", 130.0)]), &p, &e);
        assert_eq!(p["SKYAI"], 130.0);
        assert_eq!(e["SKYAI"], 100.0);
        let (p, _) = track_positions(&book(&[("SKYAI", 110.0)]), &p, &e);
        assert_eq!(p["SKYAI"], 130.0); // peak does not fall back
    }

    #[test]
    fn track_drops_closed() {
        let (p, e) = track_positions(&Book::new(), &book(&[("SKYAI", 100.0)]), &book(&[("SKYAI", 100.0)]));
        assert!(p.is_empty() && e.is_empty());
    }

    #[test]
    fn trailing_stop_and_stop_loss_fire() {
        let c = cfg();
        assert_eq!(stop_exits(&book(&[("S", 85.0)]), &book(&[("S", 100.0)]), &book(&[("S", 100.0)]), &c),
                   BTreeSet::from(["S".to_string()])); // 15% off peak
        assert_eq!(stop_exits(&book(&[("S", 87.0)]), &book(&[("S", 100.0)]), &book(&[("S", 100.0)]), &c),
                   BTreeSet::from(["S".to_string()])); // 13% underwater > 12% stop
    }

    #[test]
    fn winner_runs_no_exit() {
        let exits = stop_exits(&book(&[("S", 110.0)]), &book(&[("S", 120.0)]), &book(&[("S", 100.0)]), &cfg());
        assert!(exits.is_empty());
    }

    #[test]
    fn dynamic_sells_stopped() {
        let plan = dynamic_plan(&book(&[("SKYAI", 250.0), ("USDT", 750.0)]),
            &Book::new(), &BTreeSet::from(["SKYAI".to_string()]), &cfg());
        assert!(plan.iter().any(|s| s.src == "SKYAI" && (s.usd - 250.0).abs() < 1e-6));
    }

    #[test]
    fn dynamic_trims_runaway() {
        let h = book(&[("SKYAI", 400.0), ("USDT", 600.0)]); // 40% of $1000, above 28% ceiling
        let plan = dynamic_plan(&h, &Book::new(), &BTreeSet::new(), &cfg());
        let sells: Vec<&Swap> = plan.iter().filter(|s| s.src == "SKYAI").collect();
        assert_eq!(sells.len(), 1);
        assert!((sells[0].usd - 150.0).abs() < 1e-6); // trimmed back to max_token ($250)
        assert!(worst_case_drawdown(&project_weights(&h, &plan)) < 0.30);
    }

    #[test]
    fn dynamic_lets_winner_run_below_hard_cap() {
        let h = book(&[("SKYAI", 270.0), ("USDT", 730.0)]); // 27% < 28% ceiling
        let plan = dynamic_plan(&h, &Book::new(), &BTreeSet::new(), &cfg());
        assert!(!has_sell(&plan, "SKYAI"));
    }

    #[test]
    fn dynamic_deploys_capped() {
        let c = cfg();
        let plan = dynamic_plan(&book(&[("USDT", 1000.0)]), &book(&[("TAG", 1.0), ("SIREN", 0.8)]), &BTreeSet::new(), &c);
        assert!(!plan.is_empty() && plan.iter().all(|s| s.src == SETTLEMENT));
        assert!(plan.iter().all(|s| s.usd <= c.max_token * 1000.0 + 1e-6));
        assert!(worst_case_drawdown(&project_weights(&book(&[("USDT", 1000.0)]), &plan)) < 0.30);
    }
}
