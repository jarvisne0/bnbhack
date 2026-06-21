//! Thin, side-effecting wrapper over the Trust Wallet Agent Kit CLI — the only component that
//! touches the chain. Every response is parsed into typed structs; a parse/IO failure returns an
//! Err that the caller maps to a defined safe action (hold, skip, or treat a token as untradeable),
//! never an undefined crash. Quotes use --quote-only (no tx, no password); execution requires the
//! dry-run guard to be explicitly off.

use std::collections::BTreeMap;
use std::process::Command;

use serde::Deserialize;

use crate::risk::Book;

pub struct Twak {
    pub chain: String,
    pub quote_only: bool,
    sim: Option<Book>,
}

#[derive(Debug)]
pub struct TwakError(pub String);

impl std::fmt::Display for TwakError {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

type R<T> = Result<T, TwakError>;

#[derive(Deserialize)]
struct TokenBalance {
    #[serde(alias = "ticker")]
    symbol: Option<String>,
    #[serde(alias = "usdValue", alias = "totalUsd", alias = "value")]
    usd: Option<f64>,
}

#[derive(Deserialize)]
struct Balance {
    symbol: Option<String>,
    #[serde(rename = "totalUsd")]
    total_usd: Option<f64>,
    #[serde(default)]
    tokens: Vec<TokenBalance>,
}

#[derive(Deserialize)]
pub struct Quote {
    pub output: Option<String>,
    #[serde(rename = "minReceived")]
    pub min_received: Option<String>,
    #[serde(rename = "priceImpact")]
    pub price_impact: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct PriceHistory {
    #[serde(rename = "priceUsd")]
    price_usd: Option<serde_json::Value>,
    #[serde(default)]
    history: Vec<PricePoint>,
}

#[derive(Deserialize)]
struct PricePoint {
    price: Option<serde_json::Value>,
}

fn as_f64(v: &serde_json::Value) -> Option<f64> {
    v.as_f64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

pub fn impact_pct(q: &Quote) -> f64 {
    q.price_impact.as_ref().and_then(as_f64).unwrap_or(0.0)
}

impl Twak {
    pub fn new(chain: &str, quote_only: bool) -> Twak {
        Twak { chain: chain.to_string(), quote_only, sim: None }
    }

    /// Inject simulated holdings (dry-run only) to exercise the loop before funding.
    pub fn with_sim(chain: &str, equity: f64) -> Twak {
        Twak {
            chain: chain.to_string(),
            quote_only: true,
            sim: Some(BTreeMap::from([("USDT".to_string(), equity)])),
        }
    }

    fn run(&self, args: &[&str]) -> R<serde_json::Value> {
        let out = Command::new("twak")
            .args(args)
            .output()
            .map_err(|e| TwakError(format!("twak spawn failed: {e}")))?;
        let text = String::from_utf8_lossy(&out.stdout);
        let i = text.find('{').ok_or_else(|| {
            TwakError(format!("no JSON in `{}`: {}", args.join(" "), text.trim().chars().take(160).collect::<String>()))
        })?;
        serde_json::from_str(&text[i..])
            .map_err(|e| TwakError(format!("bad JSON in `{}`: {e}", args.join(" "))))
    }

    /// {symbol: usd} on BSC, native first then tokens, dropping zero values.
    pub fn holdings(&self) -> R<Book> {
        if let Some(s) = &self.sim {
            return Ok(s.clone());
        }
        let v = self.run(&["wallet", "balance", "--chain", &self.chain, "--json"])?;
        let b: Balance = serde_json::from_value(v).map_err(|e| TwakError(format!("balance shape: {e}")))?;
        let mut h = Book::new();
        let nat = b.total_usd.unwrap_or(0.0);
        if nat > 0.0 {
            h.insert(b.symbol.unwrap_or_else(|| "BNB".to_string()), nat);
        }
        for t in b.tokens {
            if let (Some(sym), Some(usd)) = (t.symbol, t.usd) {
                if usd != 0.0 {
                    *h.entry(sym).or_insert(0.0) += usd;
                }
            }
        }
        Ok(h)
    }

    /// Return over the window = priceUsd / earliest price - 1. None if history is thin.
    pub fn momentum(&self, contract: &str) -> Option<f64> {
        let v = self.run(&["price", contract, "--chain", &self.chain, "--history", "day", "--json"]).ok()?;
        let ph: PriceHistory = serde_json::from_value(v).ok()?;
        let now = ph.price_usd.as_ref().and_then(as_f64)?;
        let first = ph.history.first().and_then(|p| p.price.as_ref()).and_then(as_f64)?;
        if first == 0.0 {
            return None;
        }
        Some(now / first - 1.0)
    }

    pub fn quote(&self, src: &str, dst: &str, usd: f64, slippage: f64) -> R<Quote> {
        let usd_s = format!("{usd:.6}");
        let slip_s = format!("{:.4}", slippage * 100.0);
        let v = self.run(&[
            "swap", src, dst, "--usd", &usd_s, "--chain", &self.chain,
            "--slippage", &slip_s, "--quote-only", "--json",
        ])?;
        serde_json::from_value(v).map_err(|e| TwakError(format!("quote shape: {e}")))
    }

    /// Honeypot/liquidity gate: a token must quote a SELL back to USDT to be tradeable.
    pub fn sellable(&self, contract: &str, slippage: f64) -> bool {
        match self.quote(contract, "USDT", 25.0, slippage) {
            Ok(q) => q.output.is_some() && impact_pct(&q) < slippage * 100.0,
            Err(_) => false,
        }
    }

    /// Execute a swap. Refuses unless the dry-run guard is explicitly off.
    pub fn swap(&self, src: &str, dst: &str, usd: f64, slippage: f64, password: Option<&str>) -> R<serde_json::Value> {
        if self.quote_only {
            return Err(TwakError("swap() called while quote_only — dry-run guard active".into()));
        }
        let usd_s = format!("{usd:.6}");
        let slip_s = format!("{:.4}", slippage * 100.0);
        let mut args = vec![
            "swap", src, dst, "--usd", &usd_s, "--chain", &self.chain,
            "--slippage", &slip_s, "--json",
        ];
        if let Some(p) = password {
            args.push("--password");
            args.push(p);
        }
        self.run(&args)
    }
}
