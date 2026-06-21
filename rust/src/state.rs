//! Persisted control state (high-water mark, cooldown, per-position peaks/entries). Survives
//! restarts so the breaker and stops are not reset by a crash. A missing or unreadable file
//! yields a fresh zero state — a defined, safe default, never a panic.

use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::risk::Book;

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct State {
    #[serde(default)]
    pub hwm: f64,
    #[serde(default)]
    pub cooldown_until: f64,
    #[serde(default)]
    pub last_trade_ts: f64,
    #[serde(default)]
    pub peaks: Book,
    #[serde(default)]
    pub entries: Book,
}

impl State {
    pub fn load(path: &Path) -> State {
        fs::read_to_string(path)
            .ok()
            .and_then(|s| serde_json::from_str(&s).ok())
            .unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        fs::write(path, serde_json::to_string_pretty(self).unwrap())
    }
}
