//! CoinMarketCap Pro client — the deterministic data layer. One numeric signal feeds the loop:
//! per-token `trending` = 24h volume change %. The market Fear & Greed index is read as logged
//! context only. The key comes from env CMC_API_KEY or a 0600 key file — never hard-coded.

use std::collections::BTreeMap;
use std::path::PathBuf;

use crate::config::CMC_IDS;

const BASE: &str = "https://pro-api.coinmarketcap.com";

pub struct Cmc {
    key: String,
}

fn load_key() -> Result<String, String> {
    if let Ok(k) = std::env::var("CMC_API_KEY") {
        if !k.trim().is_empty() {
            return Ok(k.trim().to_string());
        }
    }
    let mut candidates: Vec<PathBuf> = vec![];
    if let Ok(p) = std::env::var("CMC_KEY_FILE") {
        candidates.push(PathBuf::from(p));
    }
    if let Ok(home) = std::env::var("HOME") {
        candidates.push(PathBuf::from(home).join(".config/bnbagent/cmc_key"));
    }
    for p in candidates {
        if let Ok(s) = std::fs::read_to_string(&p) {
            return Ok(s.trim().to_string());
        }
    }
    Err("no CMC key (set CMC_API_KEY or ~/.config/bnbagent/cmc_key)".into())
}

impl Cmc {
    pub fn new() -> Result<Cmc, String> {
        Ok(Cmc { key: load_key()? })
    }

    fn get(&self, path: &str, query: &[(&str, &str)]) -> Result<serde_json::Value, String> {
        let mut req = ureq::get(&format!("{BASE}{path}"))
            .set("X-CMC_PRO_API_KEY", &self.key)
            .set("Accept", "application/json");
        for (k, v) in query {
            req = req.query(k, v);
        }
        let j: serde_json::Value = req
            .call()
            .map_err(|e| format!("CMC {path}: {e}"))?
            .into_json()
            .map_err(|e| format!("CMC {path} body: {e}"))?;
        let code = j["status"]["error_code"].as_i64()
            .or_else(|| j["status"]["error_code"].as_str().and_then(|s| s.parse().ok()))
            .unwrap_or(0);
        if code != 0 {
            return Err(format!("CMC {path}: {}", j["status"]["error_message"]));
        }
        Ok(j["data"].clone())
    }

    /// {value, classification} — market regime context, logged not traded.
    pub fn fear_greed(&self) -> Result<(i64, String), String> {
        let d = self.get("/v3/fear-and-greed/latest", &[])?;
        Ok((
            d["value"].as_i64().unwrap_or(50),
            d["value_classification"].as_str().unwrap_or("").to_string(),
        ))
    }

    /// {token: {"trending": volume_change_24h}} for the pinned meme ids.
    pub fn heat(&self) -> Result<BTreeMap<String, BTreeMap<String, f64>>, String> {
        let ids: Vec<String> = CMC_IDS.iter().map(|(_, i)| i.to_string()).collect();
        let by_id: BTreeMap<String, &str> = CMC_IDS.iter().map(|(t, i)| (i.to_string(), *t)).collect();
        let data = self.get("/v2/cryptocurrency/quotes/latest", &[("id", &ids.join(",")), ("convert", "USD")])?;
        let mut out = BTreeMap::new();
        if let Some(obj) = data.as_object() {
            for (cid, entry) in obj {
                if let Some(tok) = by_id.get(cid) {
                    if let Some(v) = entry["quote"]["USD"]["volume_change_24h"].as_f64() {
                        out.insert(tok.to_string(), BTreeMap::from([("trending".to_string(), v)]));
                    }
                }
            }
        }
        Ok(out)
    }
}
