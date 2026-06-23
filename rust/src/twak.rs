//! Thin, side-effecting wrapper over the Trust Wallet Agent Kit CLI — the only component that
//! touches the chain. Every response is parsed into typed structs; a parse/IO failure returns an
//! Err that the caller maps to a defined safe action (hold, skip, or treat a token as untradeable),
//! never an undefined crash. Quotes use --quote-only (no tx, no password); execution requires the
//! dry-run guard to be explicitly off.

use std::collections::BTreeMap;
use std::process::Command;

use serde::Deserialize;

use crate::config::SETTLEMENT;
use crate::risk::Book;

const BSC_RPC: &str = "https://bsc-dataseed.binance.org";

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
struct PortfolioItem {
    #[serde(alias = "ticker")]
    symbol: Option<String>,
    #[serde(rename = "usdValue")]
    usd_value: Option<f64>,
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
            sim: Some(BTreeMap::from([(SETTLEMENT.to_string(), equity)])),
        }
    }

    fn run(&self, args: &[&str]) -> R<serde_json::Value> {
        let out = Command::new("twak")
            .args(args)
            .output()
            .map_err(|e| TwakError(format!("twak spawn failed: {e}")))?;
        let text = String::from_utf8_lossy(&out.stdout);
        let i = text.find(|c: char| c == '{' || c == '[').ok_or_else(|| {
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
        // `portfolio` prices every asset (native + each token); `balance` only prices the native leg,
        // so a USDC-funded wallet would read as empty. The risk engine is USD-denominated.
        let v = self.run(&["wallet", "portfolio", "--chains", &self.chain, "--json"])?;
        let items: Vec<PortfolioItem> =
            serde_json::from_value(v).map_err(|e| TwakError(format!("portfolio shape: {e}")))?;
        let mut h = Book::new();
        for it in items {
            if let (Some(sym), Some(usd)) = (it.symbol, it.usd_value) {
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

    /// BSC wallet address for this chain (decrypts the wallet — needs the password in env).
    pub fn address(&self) -> Option<String> {
        let v = self.run(&["wallet", "address", "--chain", &self.chain, "--json"]).ok()?;
        v.get("address")?.as_str().map(str::to_string)
    }

    /// Current spot price in USD for a token, via twak's price feed. Retries: a transient miss here
    /// would drop a held position from the book and understate equity → a spurious DD breaker.
    pub fn spot_usd(&self, contract: &str) -> Option<f64> {
        for _ in 0..3 {
            if let Ok(v) = self.run(&["price", contract, "--chain", &self.chain, "--history", "day", "--json"]) {
                if let Ok(ph) = serde_json::from_value::<PriceHistory>(v) {
                    if let Some(p) = ph.price_usd.as_ref().and_then(as_f64) {
                        return Some(p);
                    }
                }
            }
        }
        None
    }

    /// Raw BEP-20 `balanceOf` in token units (assumes 18 decimals — true for the meme universe),
    /// read from a public BSC RPC. twak's portfolio lists only whitelisted tokens, so a held meme is
    /// invisible to it; this lets the risk engine value its own positions.
    pub fn token_balance(&self, contract: &str, holder: &str) -> Option<f64> {
        let data = format!("0x70a08231000000000000000000000000{}", holder.trim_start_matches("0x"));
        let body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{{"to":"{contract}","data":"{data}"}},"latest"]}}"#
        );
        for _ in 0..3 {
            let Ok(out) = Command::new("curl")
                .args(["-s", "--max-time", "15", "-X", "POST", BSC_RPC,
                       "-H", "Content-Type: application/json", "--data", &body])
                .output()
            else {
                continue;
            };
            if let Ok(v) = serde_json::from_slice::<serde_json::Value>(&out.stdout) {
                if let Some(hex) = v.get("result").and_then(|r| r.as_str()) {
                    if let Ok(raw) = u128::from_str_radix(hex.trim_start_matches("0x"), 16) {
                        return Some(raw as f64 / 1e18);
                    }
                }
            }
        }
        None
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

    /// Honeypot/liquidity gate: a token must quote a SELL back to the settlement stable to be tradeable.
    pub fn sellable(&self, contract: &str, slippage: f64) -> bool {
        match self.quote(contract, SETTLEMENT, 25.0, slippage) {
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
