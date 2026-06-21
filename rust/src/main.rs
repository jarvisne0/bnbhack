//! BNB Hack Track-1 agent — single deterministic control binary.
//!
//!   bnbagent                      # real wallet, quote-only (dry-run)
//!   bnbagent --sim-equity 1000    # inject $1000 USDT to exercise the loop pre-funding
//!   bnbagent --live --password .. # ARM: execute real swaps (funded wallet)
//!
//! One pass per invocation: read portfolio -> update HWM -> (breaker | cooldown | normal) ->
//! slippage-checked swaps. Every failure mode is defined: bad input -> safe hold, invariant
//! breach -> halt. Run it on a 2-hour cron for the competition cadence.

mod cmc;
mod config;
mod risk;
mod selector;
mod signals;
mod state;
mod twak;

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use config::{Config, HIGHVOL, SETTLEMENT};
use risk::{
    breaker_tripped, drawdown, dynamic_plan, equity, project_weights, rebalance_plan, risk_held,
    stop_exits, track_positions, worst_case_drawdown, Book, Decision, Swap,
};
use selector::{select, Signals};
use state::State;
use twak::{impact_pct, Twak};

struct Args {
    live: bool,
    password: Option<String>,
    sim_equity: Option<f64>,
    no_cmc: bool,
    chain: String,
}

fn parse_args() -> Args {
    let mut a = Args { live: false, password: None, sim_equity: None, no_cmc: false, chain: "bsc".into() };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--live" => a.live = true,
            "--no-cmc" => a.no_cmc = true,
            "--password" => a.password = it.next(),
            "--sim-equity" => a.sim_equity = it.next().and_then(|v| v.parse().ok()),
            "--chain" => a.chain = it.next().unwrap_or_else(|| "bsc".into()),
            other => {
                eprintln!("unknown arg: {other}");
                std::process::exit(2);
            }
        }
    }
    if a.live && a.sim_equity.is_some() {
        eprintln!("--sim-equity is dry-run only; refusing to fake holdings while live");
        std::process::exit(2);
    }
    a
}

fn now_secs() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

fn resolve(token: &str, contracts: &BTreeMap<String, String>) -> String {
    contracts.get(token).cloned().unwrap_or_else(|| token.to_string())
}

/// CMC + momentum picks among HIGHVOL names that pass the two-way sellability gate.
fn select_picks(tw: &Twak, cfg: &Config, contracts: &BTreeMap<String, String>, cmc: &Signals) -> Book {
    let cands: BTreeMap<String, String> = HIGHVOL
        .iter()
        .filter_map(|t| contracts.get(*t).map(|c| (t.to_string(), c.clone())))
        .collect();
    let mut risk_ok = BTreeMap::new();
    for (t, c) in &cands {
        risk_ok.insert(t.clone(), tw.sellable(c, cfg.slip_for(t)));
    }
    let sigs = signals::gather(tw, &cands, cmc);
    select(&sigs, &risk_ok, cfg)
}

/// Deterministic decision dump over fixed scenarios, as canonical strings, for byte-for-byte
/// comparison against the Python engine (scripts/parity.py). No I/O, no live data.
fn parity_dump() -> serde_json::Value {
    use std::collections::BTreeSet;
    let cfg = Config::default();
    let fmt_plan = |p: &[Swap]| -> Vec<String> {
        p.iter().map(|s| format!("{}>{}:{:.6}", s.src, s.dst, s.usd)).collect()
    };
    let fmt_book = |b: &Book| -> Vec<String> {
        b.iter().map(|(t, v)| format!("{t}:{v:.6}")).collect()
    };
    let book = |pairs: &[(&str, f64)]| -> Book { pairs.iter().map(|(t, v)| (t.to_string(), *v)).collect() };

    // A: select on fixed signals
    let sigs: Signals = [
        ("SKYAI", &[("momentum", 0.20_f64), ("trending", 50.0)][..]),
        ("TAG", &[("momentum", 0.10), ("trending", 80.0)][..]),
        ("SIREN", &[("momentum", -0.05), ("trending", 10.0)][..]),
        ("MYX", &[("momentum", 0.30), ("trending", -20.0)][..]),
        ("DEXE", &[("momentum", 0.15), ("trending", 62.0)][..]),
    ].iter().map(|(t, ms)| (t.to_string(), ms.iter().map(|(m, v)| (m.to_string(), *v)).collect())).collect();
    let risk_ok: BTreeMap<String, bool> = ["SKYAI", "TAG", "SIREN", "MYX", "DEXE"].iter().map(|t| (t.to_string(), true)).collect();
    let picks = select(&sigs, &risk_ok, &cfg);

    // B: fresh deploy  C: trim runaway  D: stop a loser then redeploy  E: full rotation
    let b = dynamic_plan(&book(&[("USDT", 1000.0)]), &picks, &BTreeSet::new(), &cfg);
    let c_h = book(&[("SKYAI", 400.0), ("TAG", 150.0), ("USDT", 450.0)]);
    let c = dynamic_plan(&c_h, &picks, &BTreeSet::new(), &cfg);
    let d_h = book(&[("SKYAI", 200.0), ("TAG", 200.0), ("USDT", 600.0)]);
    let d = dynamic_plan(&d_h, &picks, &BTreeSet::from(["TAG".to_string()]), &cfg);
    let e = rebalance_plan(&book(&[("SKYAI", 250.0), ("TAG", 250.0), ("USDT", 500.0)]), &BTreeMap::from([(SETTLEMENT.to_string(), 1.0)]), &cfg);

    serde_json::json!({
        "A_select": fmt_book(&picks),
        "B_deploy": fmt_plan(&b),
        "B_weights": fmt_book(&project_weights(&book(&[("USDT", 1000.0)]), &b)),
        "C_trim": fmt_plan(&c),
        "C_weights": fmt_book(&project_weights(&c_h, &c)),
        "D_stop_redeploy": fmt_plan(&d),
        "E_rotation": fmt_plan(&e),
    })
}

fn main() {
    if std::env::args().any(|a| a == "--parity") {
        println!("{}", serde_json::to_string_pretty(&parity_dump()).unwrap());
        return;
    }
    let args = parse_args();
    let cfg = Config::default();
    let contracts = config::load_contracts();
    let state_path = PathBuf::from("state.json");

    // CMC signals (degrade to momentum-only if unavailable — never block trading).
    let mut cmc_sig = Signals::new();
    let mut regime: Option<(i64, String)> = None;
    if !args.no_cmc {
        match cmc::Cmc::new().and_then(|c| {
            let heat = c.heat()?;
            let fg = c.fear_greed().ok();
            Ok((heat, fg))
        }) {
            Ok((heat, fg)) => {
                cmc_sig = heat;
                regime = fg;
            }
            Err(e) => eprintln!("# CMC unavailable ({e}); selecting on momentum alone"),
        }
    }

    let tw = match args.sim_equity {
        Some(eq) => Twak::with_sim(&args.chain, eq),
        None => Twak::new(&args.chain, !args.live),
    };

    let mut st = State::load(&state_path);
    let now = now_secs();

    let holdings = match tw.holdings() {
        Ok(h) => h,
        Err(e) => {
            print_result(&serde_json::json!({"action": "error", "reason": e.to_string(), "swaps": []}), &regime);
            std::process::exit(1);
        }
    };
    let eq = equity(&holdings);
    if eq < 1.0 {
        print_result(
            &serde_json::json!({"action": "idle", "reason": format!("portfolio ${eq:.2} < $1 — FUND WALLET"),
                                "equity": eq, "swaps": []}),
            &regime,
        );
        return;
    }

    st.hwm = st.hwm.max(eq);
    let dd = drawdown(eq, st.hwm);
    let held = risk_held(&holdings);
    let (peaks, entries) = track_positions(&held, &st.peaks, &st.entries);
    st.peaks = peaks;
    st.entries = entries;

    let decision = if breaker_tripped(eq, st.hwm, &cfg) {
        Decision::Breaker
    } else if now < st.cooldown_until {
        Decision::Cooldown
    } else {
        Decision::Normal
    };

    let usdt_only: Book = BTreeMap::from([(SETTLEMENT.to_string(), 1.0)]);
    let (plan, reason): (Vec<Swap>, String) = match decision {
        Decision::Breaker => {
            st.cooldown_until = now + cfg.cooldown_h * 3600.0;
            st.peaks.clear();
            st.entries.clear();
            (rebalance_plan(&holdings, &usdt_only, &cfg),
             format!("DD BREAKER {:.1}% >= {:.0}%", dd * 100.0, cfg.dd_stop * 100.0))
        }
        Decision::Cooldown => {
            st.peaks.clear();
            st.entries.clear();
            (rebalance_plan(&holdings, &usdt_only, &cfg),
             format!("cooldown ({:.1}h left)", (st.cooldown_until - now) / 3600.0))
        }
        Decision::Normal => {
            let exits = stop_exits(&held, &st.peaks, &st.entries, &cfg);
            let picks = select_picks(&tw, &cfg, &contracts, &cmc_sig);
            let plan = dynamic_plan(&holdings, &picks, &exits, &cfg);
            for t in &exits {
                st.peaks.remove(t);
                st.entries.remove(t);
            }
            let picks_v: Vec<&String> = picks.keys().collect();
            let stopped = if exits.is_empty() { String::new() } else { format!(", stopped {:?}", exits) };
            let reason = if picks.is_empty() && exits.is_empty() {
                "no vehicle passed risk/selection".to_string()
            } else {
                format!("hold/deploy: picks {:?}{}", picks_v, stopped)
            };
            (plan, reason)
        }
    };

    let target = project_weights(&holdings, &plan);
    assert!(worst_case_drawdown(&target) < 0.30, "post-trade book violates the 30% single-rug guard");

    let mut swaps = vec![];
    for s in &plan {
        let risk_tok = if s.src == SETTLEMENT { &s.dst } else { &s.src };
        let slip = cfg.slip_for(risk_tok);
        let (src, dst) = (resolve(&s.src, &contracts), resolve(&s.dst, &contracts));
        match tw.quote(&src, &dst, s.usd, slip) {
            Err(e) => swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": format!("quote-failed: {e}")})),
            Ok(q) => {
                let impact = impact_pct(&q) / 100.0;
                if impact > slip {
                    swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd,
                        "status": format!("slippage {:.2}% > cap {:.2}% — skipped", impact * 100.0, slip * 100.0)}));
                } else if tw.quote_only {
                    swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": "quoted",
                        "out": q.output, "minReceived": q.min_received}));
                } else {
                    match tw.swap(&src, &dst, s.usd, slip, args.password.as_deref()) {
                        Ok(tx) => {
                            st.last_trade_ts = now;
                            swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": "executed", "tx": tx}));
                        }
                        Err(e) => swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": format!("swap-failed: {e}")})),
                    }
                }
            }
        }
    }

    let _ = st.save(&state_path);
    let result = serde_json::json!({
        "action": "rebalance", "reason": reason, "equity": (eq * 100.0).round() / 100.0,
        "hwm": (st.hwm * 100.0).round() / 100.0, "drawdown": (dd * 10000.0).round() / 10000.0,
        "target": target, "swaps": swaps,
    });
    print_result(&result, &regime);
}

fn print_result(result: &serde_json::Value, regime: &Option<(i64, String)>) {
    let mut r = result.clone();
    if let Some((v, c)) = regime {
        r["fear_greed"] = serde_json::json!({"value": v, "classification": c});
    }
    println!("{}", serde_json::to_string_pretty(&r).unwrap());
}
