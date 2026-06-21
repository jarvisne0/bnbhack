//! Live signal gathering. Momentum comes from twak price history (always available); CMC trending
//! is merged in when present. The selector z-scores whatever metrics exist, so the agent degrades
//! gracefully to momentum-only if CMC is briefly unavailable.

use std::collections::BTreeMap;

use crate::selector::Signals;
use crate::twak::Twak;

pub fn gather(tw: &Twak, cands: &BTreeMap<String, String>, cmc: &Signals) -> Signals {
    let mut out = Signals::new();
    for (tok, contract) in cands {
        let mut sig = BTreeMap::new();
        if let Some(m) = tw.momentum(contract) {
            sig.insert("momentum".to_string(), m);
        }
        if let Some(c) = cmc.get(tok) {
            for k in ["sentiment", "trending"] {
                if let Some(v) = c.get(k) {
                    sig.insert(k.to_string(), *v);
                }
            }
        }
        if !sig.is_empty() {
            out.insert(tok.clone(), sig);
        }
    }
    out
}
