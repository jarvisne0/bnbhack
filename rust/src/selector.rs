//! Vehicle selection — an honest live heuristic, NOT a backtested alpha. Ranks the high-variance
//! universe by how much each name is heating up (CMC trending + momentum) and picks the top N to
//! spread convexity across. Pure given its inputs, so the choice is reproducible and logged.

use std::collections::BTreeMap;

use crate::config::Config;
use crate::risk::Book;

pub type Signals = BTreeMap<String, BTreeMap<String, f64>>;

const METRICS: [&str; 3] = ["sentiment", "trending", "momentum"];

/// Z-score a metric across the candidate set (0 if degenerate).
fn zscore(values: &BTreeMap<String, f64>) -> BTreeMap<String, f64> {
    if values.len() < 2 {
        return values.keys().map(|k| (k.clone(), 0.0)).collect();
    }
    let n = values.len() as f64;
    let mean = values.values().sum::<f64>() / n;
    let var = values.values().map(|x| (x - mean).powi(2)).sum::<f64>() / n;
    let sd = var.sqrt();
    if sd == 0.0 {
        return values.keys().map(|k| (k.clone(), 0.0)).collect();
    }
    values.iter().map(|(k, v)| (k.clone(), (v - mean) / sd)).collect()
}

/// Combine per-token signals into a convex score = mean of the available z-scored metrics.
pub fn score(signals: &Signals) -> BTreeMap<String, f64> {
    let zs: Vec<BTreeMap<String, f64>> = METRICS
        .iter()
        .map(|m| {
            let vals: BTreeMap<String, f64> = signals
                .iter()
                .filter_map(|(t, s)| s.get(*m).map(|v| (t.clone(), *v)))
                .collect();
            zscore(&vals)
        })
        .collect();
    let mut out = BTreeMap::new();
    for t in signals.keys() {
        let comp: Vec<f64> = zs.iter().filter_map(|z| z.get(t).copied()).collect();
        let v = if comp.is_empty() { 0.0 } else { comp.iter().sum::<f64>() / comp.len() as f64 };
        out.insert(t.clone(), v);
    }
    out
}

/// Pick the top n_vehicles risk-passing names as {token: positive score}. Scores are shifted to be
/// strictly positive so the sizer treats them as convex weights; risk failures are excluded.
pub fn select(signals: &Signals, risk_ok: &BTreeMap<String, bool>, cfg: &Config) -> Book {
    let passing: Signals = signals
        .iter()
        .filter(|(t, _)| *risk_ok.get(*t).unwrap_or(&false))
        .map(|(t, s)| (t.clone(), s.clone()))
        .collect();
    let scored = score(&passing);
    if scored.is_empty() {
        return Book::new();
    }
    // rank by score desc, breaking ties by token so the choice is canonical (Python parity)
    let mut ranked: Vec<(String, f64)> = scored.into_iter().collect();
    ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap().then(a.0.cmp(&b.0)));
    ranked.truncate(cfg.n_vehicles);
    let floor = ranked.iter().map(|(_, v)| *v).fold(f64::INFINITY, f64::min);
    ranked.into_iter().map(|(t, v)| (t, (v - floor) + 1.0)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn sig(pairs: &[(&str, &[(&str, f64)])]) -> Signals {
        pairs.iter().map(|(t, ms)| {
            (t.to_string(), ms.iter().map(|(m, v)| (m.to_string(), *v)).collect())
        }).collect()
    }
    fn all_ok(s: &Signals) -> BTreeMap<String, bool> {
        s.keys().map(|t| (t.clone(), true)).collect()
    }

    #[test]
    fn excludes_risk_failures() {
        let s = sig(&[("SKYAI", &[("momentum", 0.1)]), ("TAG", &[("momentum", 0.2)])]);
        let ok = BTreeMap::from([("SKYAI".to_string(), true), ("TAG".to_string(), false)]);
        let picks = select(&s, &ok, &Config::default());
        assert!(picks.contains_key("SKYAI") && !picks.contains_key("TAG"));
    }

    #[test]
    fn picks_are_positive() {
        let s = sig(&[("A", &[("momentum", 0.3)]), ("B", &[("momentum", -0.1)]), ("C", &[("momentum", 0.1)])]);
        let picks = select(&s, &all_ok(&s), &Config::default());
        assert!(picks.values().all(|v| *v > 0.0));
    }

    #[test]
    fn caps_at_n_vehicles() {
        let s = sig(&[("A", &[("momentum", 0.1)]), ("B", &[("momentum", 0.2)]), ("C", &[("momentum", 0.3)]),
                      ("D", &[("momentum", 0.4)]), ("E", &[("momentum", 0.5)]), ("F", &[("momentum", 0.6)])]);
        let picks = select(&s, &all_ok(&s), &Config::default());
        assert!(picks.len() <= Config::default().n_vehicles);
    }

    #[test]
    fn score_handles_missing_metrics() {
        let s = sig(&[("A", &[("momentum", 0.2), ("sentiment", 1.0)]), ("B", &[("momentum", 0.1)])]);
        let out = score(&s);
        assert_eq!(out.keys().cloned().collect::<BTreeSet<_>>(),
                   BTreeSet::from(["A".to_string(), "B".to_string()]));
    }
}
