//! Append-only equity / drawdown ledger for competition monitoring. Both the 2-hour trade pass
//! and the hourly `--log-only` pass feed it, giving a continuous trail against the 30% drawdown
//! DQ line. A write failure is reported on stderr, never fatal — monitoring must not abort trading.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

use crate::config::{GAS_MIN_USD, GAS_WALLET};
use crate::risk::Book;

const WARN_DD: f64 = 0.20; // shout once within ~5pts of the 25% breaker / 10pts of the 30% DQ

/// Low-gas warning string, or None when the tank is fine. The agent wallet funds its own swaps,
/// so a dry tank halts trading silently; names role + exact fund address per the gas-monitoring rule.
pub fn gas_alert(holdings: &Book) -> Option<String> {
    let bnb = holdings.get("BNB").copied().unwrap_or(0.0);
    (bnb < GAS_MIN_USD)
        .then(|| format!("# WARN gas low: BNB ${bnb:.2} < ${GAS_MIN_USD:.2} — fund agent gas wallet {GAS_WALLET}"))
}

/// Append one `ts_utc,unix,equity,hwm,drawdown,action` row (writing the header on first use).
pub fn log_equity(path: &Path, unix: f64, eq: f64, hwm: f64, dd: f64, action: &str) {
    if let Some(p) = path.parent() {
        let _ = fs::create_dir_all(p);
    }
    let fresh = !path.exists();
    let line = format!(
        "{},{},{:.2},{:.2},{:.4},{}\n",
        fmt_utc(unix as i64), unix as i64, eq, hwm, dd, action.replace(',', ";")
    );
    match OpenOptions::new().create(true).append(true).open(path) {
        Ok(mut f) => {
            if fresh {
                let _ = f.write_all(b"ts_utc,unix,equity,hwm,drawdown,action\n");
            }
            if let Err(e) = f.write_all(line.as_bytes()) {
                eprintln!("# equity log write failed: {e}");
            }
        }
        Err(e) => eprintln!("# equity log open failed: {e}"),
    }
    if dd >= WARN_DD {
        eprintln!("# WARN drawdown {:.1}% — approaching 25% breaker / 30% DQ", dd * 100.0);
    }
}

/// UTC `YYYY-MM-DD HH:MM:SS` from unix seconds via the civil-calendar algorithm (no extra dep,
/// deterministic). Valid for any post-epoch timestamp the competition will produce.
fn fmt_utc(secs: i64) -> String {
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if mth <= 2 { y + 1 } else { y };
    format!("{year:04}-{mth:02}-{d:02} {h:02}:{m:02}:{s:02}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fmt_utc_known() {
        assert_eq!(fmt_utc(1_700_000_000), "2023-11-14 22:13:20");
        assert_eq!(fmt_utc(0), "1970-01-01 00:00:00");
    }

    #[test]
    fn gas_alert_threshold() {
        assert!(gas_alert(&Book::from([("BNB".to_string(), 1.50)])).is_some());
        assert!(gas_alert(&Book::from([("BNB".to_string(), 5.00)])).is_none());
        assert!(gas_alert(&Book::new()).is_some()); // no BNB at all = dry tank
    }
}
