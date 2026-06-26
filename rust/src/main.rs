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
mod monitor;
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
    breaker_tripped, drawdown, dynamic_plan, equity, heartbeat_plan, project_weights, rebalance_plan,
    regime_aggression, risk_held, stop_exits, track_positions, worst_case_drawdown, Book, Decision, Swap,
};
use selector::{select, Signals};
use state::State;
use twak::{impact_pct, Twak};

struct Args {
    live: bool,
    password: Option<String>,
    sim_equity: Option<f64>,
    no_cmc: bool,
    log_only: bool,
    chain: String,
}

fn parse_args() -> Args {
    let mut a = Args { live: false, password: None, sim_equity: None, no_cmc: false, log_only: false, chain: "bsc".into() };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--live" => a.live = true,
            "--no-cmc" => a.no_cmc = true,
            "--log-only" => a.log_only = true,
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
    let b = dynamic_plan(&book(&[(SETTLEMENT,1000.0)]), &picks, &BTreeSet::new(), &cfg);
    let c_h = book(&[("SKYAI", 400.0), ("TAG", 150.0), (SETTLEMENT,450.0)]);
    let c = dynamic_plan(&c_h, &picks, &BTreeSet::new(), &cfg);
    let d_h = book(&[("SKYAI", 200.0), ("TAG", 200.0), (SETTLEMENT,600.0)]);
    let d = dynamic_plan(&d_h, &picks, &BTreeSet::from(["TAG".to_string()]), &cfg);
    let e = rebalance_plan(&book(&[("SKYAI", 250.0), ("TAG", 250.0), (SETTLEMENT,500.0)]), &BTreeMap::from([(SETTLEMENT.to_string(), 1.0)]), &cfg);
    // F: daily-activity heartbeat — round-trip the largest holding; deploy top pick when all-cash
    let f1 = heartbeat_plan(&book(&[("SKYAI", 300.0), ("TAG", 250.0), (SETTLEMENT, 450.0)]), &picks, &cfg);
    let f2 = heartbeat_plan(&book(&[(SETTLEMENT, 1000.0)]), &picks, &cfg);

    serde_json::json!({
        "A_select": fmt_book(&picks),
        "B_deploy": fmt_plan(&b),
        "B_weights": fmt_book(&project_weights(&book(&[(SETTLEMENT,1000.0)]), &b)),
        "C_trim": fmt_plan(&c),
        "C_weights": fmt_book(&project_weights(&c_h, &c)),
        "D_stop_redeploy": fmt_plan(&d),
        "E_rotation": fmt_plan(&e),
        "F_heartbeat_held": fmt_plan(&f1),
        "F_heartbeat_cash": fmt_plan(&f2),
    })
}

fn main() {
    if std::env::args().any(|a| a == "--parity") {
        println!("{}", serde_json::to_string_pretty(&parity_dump()).unwrap());
        return;
    }
    let args = parse_args();
    let mut cfg = Config::default();
    let contracts = config::load_contracts();
    let state_path = PathBuf::from("state.json");
    let log_path = PathBuf::from("logs/equity.csv");

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
    // Spot-only long book can't short a bear: scale the deploy budget down in fear (hold cash),
    // up to the configured ceiling only in greed. No regime signal -> neutral stance.
    if let Some((_, cls)) = &regime {
        cfg.aggression = regime_aggression(cls, cfg.aggression);
    }

    let tw = match args.sim_equity {
        Some(eq) => Twak::with_sim(&args.chain, eq),
        None => Twak::new(&args.chain, !args.live),
    };

    let mut st = State::load(&state_path);
    let now = now_secs();

    let mut holdings = match tw.holdings() {
        Ok(h) => h,
        Err(e) => {
            print_result(&serde_json::json!({"action": "error", "reason": e.to_string(), "swaps": []}), &regime);
            std::process::exit(1);
        }
    };
    // twak's portfolio lists only whitelisted tokens, so a held meme is invisible to it. Value each
    // universe token straight from chain (balanceOf x spot). `incomplete` flags any held position we
    // could not value this pass — acting on the resulting understated equity would spuriously trip
    // the DD breaker, so we hold instead.
    let mut incomplete = false;
    if args.sim_equity.is_none() {
        match tw.address() {
            None => incomplete = true,
            Some(addr) => {
                for t in HIGHVOL {
                    if holdings.contains_key(t) {
                        continue;
                    }
                    let Some(c) = contracts.get(t) else { continue };
                    match tw.token_balance(c, &addr) {
                        None => {
                            eprintln!("# WARN balanceOf {t} unreadable — holdings incomplete");
                            incomplete = true;
                        }
                        Some(amt) if amt > 0.0 => match tw.spot_usd(c) {
                            Some(px) => {
                                let usd = amt * px;
                                if usd > 0.01 {
                                    holdings.insert(t.to_string(), usd);
                                }
                            }
                            None => {
                                eprintln!("# WARN held {t} ({amt}) unpriceable — holdings incomplete");
                                incomplete = true;
                            }
                        },
                        Some(_) => {} // zero balance: not held
                    }
                }
            }
        }
    }
    if incomplete {
        print_result(
            &serde_json::json!({"action": "hold",
                "reason": "holdings valuation incomplete (RPC/price unavailable) — skipping to avoid acting on understated equity",
                "swaps": []}),
            &regime,
        );
        return;
    }
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
    if args.sim_equity.is_none() {
        if let Some(w) = monitor::gas_alert(&holdings) {
            eprintln!("{w}"); // real wallet only; sim has no native BNB
        }
    }
    let held = risk_held(&holdings, &cfg);
    let (peaks, entries) = track_positions(&held, &st.peaks, &st.entries);
    st.peaks = peaks;
    st.entries = entries;

    // Hourly monitor pass: record equity/drawdown only, touch no positions, never trade.
    if args.log_only {
        monitor::log_equity(&log_path, now, eq, st.hwm, dd, "monitor");
        let _ = st.save(&state_path);
        print_result(
            &serde_json::json!({"action": "monitor", "equity": (eq * 100.0).round() / 100.0,
                                "hwm": (st.hwm * 100.0).round() / 100.0,
                                "drawdown": (dd * 10000.0).round() / 10000.0, "swaps": []}),
            &regime,
        );
        return;
    }

    let decision = if breaker_tripped(eq, st.hwm, &cfg) {
        Decision::Breaker
    } else if now < st.cooldown_until {
        Decision::Cooldown
    } else {
        Decision::Normal
    };

    let action_label = match &decision {
        Decision::Breaker => "breaker",
        Decision::Cooldown => "cooldown",
        Decision::Normal => "normal",
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
            let mut plan = dynamic_plan(&holdings, &picks, &exits, &cfg);
            for t in &exits {
                st.peaks.remove(t);
                st.entries.remove(t);
            }
            let picks_v: Vec<&String> = picks.keys().collect();
            let stopped = if exits.is_empty() { String::new() } else { format!(", stopped {:?}", exits) };
            let mut reason = if picks.is_empty() && exits.is_empty() {
                "no vehicle passed risk/selection".to_string()
            } else {
                format!("hold/deploy: picks {:?}{}", picks_v, stopped)
            };
            // Activity gate: a converged book trades nothing for days, but the comp needs >=1/day.
            // If nothing else fires and the last fill is stale, force one minimal compliant swap.
            let stale_h = (now - st.last_trade_ts) / 3600.0;
            if plan.is_empty() && stale_h > cfg.heartbeat_h {
                let hb = heartbeat_plan(&holdings, &picks, &cfg);
                if !hb.is_empty() {
                    reason = format!("heartbeat: {stale_h:.0}h since last trade >= {:.0}h — forced activity swap", cfg.heartbeat_h);
                    plan = hb;
                }
            }
            (plan, reason)
        }
    };

    let target = project_weights(&holdings, &plan);
    assert!(worst_case_drawdown(&target) < 0.30, "post-trade book violates the 30% single-rug guard");

    // Unattended live runs may supply the wallet password via env (kept out of argv/ps).
    let password = args.password.clone().or_else(|| std::env::var("BNBAGENT_WALLET_PW").ok());

    let mut swaps = vec![];
    let mut executed: Vec<String> = vec![];
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
                    match tw.swap(&src, &dst, s.usd, slip, password.as_deref()) {
                        // twak returns exit 0 with an {error,...} body on failure (e.g. out of gas);
                        // only a real tx hash is a genuine fill — anything else is a failure.
                        Ok(tx) if tx.get("hash").and_then(|h| h.as_str()).is_some() => {
                            st.last_trade_ts = now;
                            let hash = tx.get("hash").and_then(|h| h.as_str()).unwrap_or("");
                            executed.push(format!("{} → {} ${:.2}  https://bscscan.com/tx/{hash}", s.src, s.dst, s.usd));
                            swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": "executed", "tx": tx}));
                        }
                        Ok(tx) => {
                            let err = tx.get("error").and_then(|e| e.as_str()).unwrap_or("no tx hash returned");
                            swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": format!("swap-failed: {err}")}));
                        }
                        Err(e) => swaps.push(serde_json::json!({"src": s.src, "dst": s.dst, "usd": s.usd, "status": format!("swap-failed: {e}")})),
                    }
                }
            }
        }
    }

    let _ = st.save(&state_path);
    monitor::log_equity(&log_path, now, eq, st.hwm, dd, action_label);
    if !executed.is_empty() {
        tg_notify(&format!(
            "🤖 <b>BNB Agent</b> — {} trade(s) [{}]\n{}\nequity ${:.2} · DD {:.1}%",
            executed.len(), action_label, executed.join("\n"), eq, dd * 100.0,
        ));
    }
    let result = serde_json::json!({
        "action": "rebalance", "reason": reason, "equity": (eq * 100.0).round() / 100.0,
        "hwm": (st.hwm * 100.0).round() / 100.0, "drawdown": (dd * 10000.0).round() / 10000.0,
        "aggression": cfg.aggression, "target": target, "swaps": swaps,
    });
    print_result(&result, &regime);
}

/// Push a trade notification to the LEECH Telegram chat (the VPS is headless). Best-effort:
/// reads BNB_TG_TOKEN + BNB_TG_CHAT from env, no-ops if unset, never blocks or fails the run.
fn tg_notify(text: &str) {
    let (Ok(token), Ok(chat)) = (std::env::var("BNB_TG_TOKEN"), std::env::var("BNB_TG_CHAT")) else {
        return;
    };
    let url = format!("https://api.telegram.org/bot{token}/sendMessage");
    let body = serde_json::json!({"chat_id": chat, "text": text, "parse_mode": "HTML"}).to_string();
    let _ = std::process::Command::new("curl")
        .args(["-s", "--max-time", "15", "-X", "POST", &url, "-H", "Content-Type: application/json", "--data", &body])
        .output();
}

fn print_result(result: &serde_json::Value, regime: &Option<(i64, String)>) {
    let mut r = result.clone();
    if let Some((v, c)) = regime {
        r["fear_greed"] = serde_json::json!({"value": v, "classification": c});
    }
    println!("{}", serde_json::to_string_pretty(&r).unwrap());
}
