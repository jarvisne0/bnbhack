//! Static config + universe. The market is efficient (eight edges tested, all ~0), so the
//! edge is the risk engine, not a signal. Every tunable lives here; the constructor asserts
//! the invariants that keep a single rug or a drawdown spiral under the 30% DQ line.

use std::collections::BTreeMap;
use std::fs;

pub const SETTLEMENT: &str = "USDT";

/// BSC-only high-variance vehicles (vol > $500k/24h). Selection is restricted to these.
pub const HIGHVOL: [&str; 6] = ["SKYAI", "BANANAS31", "TAG", "SIREN", "MYX", "DEXE"];

/// Pinned CMC ids (resolve by id, not symbol — TAG/SIREN tickers collide).
pub const CMC_IDS: [(&str, u64); 6] = [
    ("SKYAI", 36300), ("BANANAS31", 34118), ("TAG", 34958),
    ("SIREN", 35766), ("MYX", 36410), ("DEXE", 7326),
];

#[derive(Clone, Debug)]
pub struct Config {
    // HARD risk invariants (non-negotiable; protect the $24k)
    pub dd_stop: f64,       // rotate ALL to USDT at >= this drawdown (5% under the 30% DQ)
    pub max_token: f64,     // per-token entry cap: a 100% rug => <= this hit, under 30%
    pub hard_cap: f64,      // run-time ceiling: trim a winner back to max_token above this
    pub stable_floor: f64,  // always hold >= this fraction in USDT
    pub slip: f64,          // abort a swap whose quoted slippage exceeds this
    pub min_swap: f64,      // skip dust trades

    // per-position stops
    pub trail: f64,         // exit a position that falls this far from its peak value
    pub stop_loss: f64,     // exit a position this far underwater from entry

    // behaviour
    pub aggression: f64,    // target risk-on fraction (capped at 1 - stable_floor)
    pub n_vehicles: usize,  // spread convexity across this many names
    pub cooldown_h: f64,    // after a breaker trip, stay in USDT this long

    slip_overrides: BTreeMap<String, f64>,
}

impl Default for Config {
    fn default() -> Self {
        let mut slip_overrides = BTreeMap::new();
        for t in ["SIREN", "MYX", "DEXE"] {
            slip_overrides.insert(t.to_string(), 0.04);
        }
        let cfg = Config {
            dd_stop: 0.25, max_token: 0.25, hard_cap: 0.28, stable_floor: 0.20,
            slip: 0.02, min_swap: 2.0, trail: 0.15, stop_loss: 0.12,
            aggression: 0.60, n_vehicles: 4, cooldown_h: 12.0, slip_overrides,
        };
        cfg.check();
        cfg
    }
}

impl Config {
    /// Fail fast at construction — the same guards as the Python `__post_init__`.
    pub fn check(&self) {
        assert!(self.dd_stop > 0.0 && self.dd_stop < 0.30, "dd_stop must sit under the 30% DQ line");
        assert!(self.max_token > 0.0 && self.max_token <= self.hard_cap && self.hard_cap < 0.30,
                "no single token may reach the 30% rug line");
        assert!(self.stable_floor >= 0.0 && self.stable_floor < 1.0);
        assert!(self.aggression >= 0.0 && self.aggression <= 1.0 - self.stable_floor,
                "aggression must leave the stable floor");
        assert!(self.trail > 0.0 && self.trail < 1.0 && self.stop_loss > 0.0 && self.stop_loss < 1.0);
        assert!(self.n_vehicles >= 1);
    }

    pub fn slip_for(&self, token: &str) -> f64 {
        *self.slip_overrides.get(token).unwrap_or(&self.slip)
    }
}

/// BSC contract addresses for tokens twak can't resolve by symbol. Tries the path relative to
/// the working dir, then one level up (so it works from `rust/` or the repo root).
pub fn load_contracts() -> BTreeMap<String, String> {
    for p in ["data/token_contracts.json", "../data/token_contracts.json"] {
        if let Ok(s) = fs::read_to_string(p) {
            if let Ok(m) = serde_json::from_str::<BTreeMap<String, String>>(&s) {
                return m;
            }
        }
    }
    BTreeMap::new()
}
