#!/usr/bin/env bash
# Cron driver for the Rust engine. Loads secrets from 0600 files (never argv, never committed),
# then runs one pass. No-fallback: a missing binary or, in `trade` mode, a missing wallet password
# is a hard stop — it will NOT silently dry-run or skip.
#
#   run_agent.sh dry      quote-only pass (no funds, no password)        -> every 2h pre-funding
#   run_agent.sh trade    LIVE: execute real swaps (needs wallet pw)     -> every 2h once funded
#   run_agent.sh log      equity/drawdown monitor only, no trading        -> every hour
#
# Secrets (0600, gitignored): CMC key from ./.cmc_key or ~/.config/bnbagent/cmc_key;
# wallet password from ~/.config/bnbagent/wallet_pw (trade mode only).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# cron runs with a bare PATH; twak lives in the user npm prefix and the binary spawns it by name.
export PATH="$HOME/.npm-global/bin:$PATH"
BIN="$ROOT/rust/target/release/bnbagent"
MODE="${1:-dry}"

[ -x "$BIN" ] || { echo "FATAL: $BIN not built — run: (cd rust && cargo build --release)" >&2; exit 1; }

# CMC key -> env for the binary (it also reads ~/.config/bnbagent/cmc_key on its own).
if [ -f "$ROOT/.cmc_key" ]; then
  export CMC_API_KEY="$(< "$ROOT/.cmc_key")"
elif [ -f "$HOME/.config/bnbagent/cmc_key" ]; then
  export CMC_API_KEY="$(< "$HOME/.config/bnbagent/cmc_key")"
fi

case "$MODE" in
  dry)   exec "$BIN" ;;
  log)
    # read-only monitor: still needs the wallet password to read the portfolio (no --live, no tx)
    [ -f "$HOME/.config/bnbagent/wallet_pw" ] && export TWAK_WALLET_PASSWORD="$(< "$HOME/.config/bnbagent/wallet_pw")"
    exec "$BIN" --log-only --no-cmc ;;
  trade)
    PW_FILE="$HOME/.config/bnbagent/wallet_pw"
    [ -f "$PW_FILE" ] || { echo "FATAL: $PW_FILE missing — cannot arm live trading (no silent fallback)" >&2; exit 1; }
    export BNBAGENT_WALLET_PW="$(< "$PW_FILE")"
    export TWAK_WALLET_PASSWORD="$BNBAGENT_WALLET_PW"   # twak reads (portfolio) decrypt the wallet too
    if [ -f "$HOME/.config/bnbagent/tg_token" ]; then   # LEECH Telegram trade trail (headless VPS)
      export BNB_TG_TOKEN="$(< "$HOME/.config/bnbagent/tg_token")"
      export BNB_TG_CHAT="5719511428"
    fi
    exec "$BIN" --live ;;
  *) echo "usage: run_agent.sh {dry|trade|log}" >&2; exit 2 ;;
esac
