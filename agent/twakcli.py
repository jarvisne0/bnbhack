"""Thin, side-effecting wrapper over the Trust Wallet Agent Kit CLI.

The ONLY component that touches the outside world. Everything here is on `--chain bsc`.
Quotes use `--quote-only` (deterministic, no tx, no password); execution requires a
password (env TWAK_WALLET_PASSWORD / keychain when armed live). twak prints a human line
before its JSON, so we extract from the first brace.

Verified against twak 0.19.1: swap quote -> {input,output,minReceived,provider,priceImpact};
balance -> {symbol,totalUsd,tokens:[]}; `risk` needs a security-scoped key we don't have
(403) so it's a SOFT check — the hard sellability gate is a two-way round-trip quote.
"""
from __future__ import annotations

import json
import subprocess

from .config import SETTLEMENT


class TwakError(RuntimeError):
    pass


class Twak:
    def __init__(self, chain: str = "bsc", base_cmd: tuple[str, ...] = ("twak",),
                 quote_only: bool = True, timeout: int = 90):
        self.chain = chain
        self.base = list(base_cmd)
        self.quote_only = quote_only  # global dry-run guard: True => never executes a tx
        self.timeout = timeout

    def _run(self, args: list[str]) -> dict | list:
        proc = subprocess.run(self.base + args, capture_output=True, text=True,
                              timeout=self.timeout)
        out = proc.stdout
        starts = [p for p in (out.find("{"), out.find("[")) if p >= 0]
        if not starts:
            raise TwakError(f"no JSON in `{' '.join(args)}`: {out.strip()[:200]} "
                            f"{proc.stderr.strip()[:200]}")
        i = min(starts)
        try:
            return json.loads(out[i:])
        except json.JSONDecodeError as e:
            raise TwakError(f"bad JSON in `{' '.join(args)}`: {e}: {out[i:][:200]}")

    # --- reads ---
    def holdings(self) -> dict[str, float]:
        """{symbol: usd} on BSC. `portfolio` prices every asset (native + each token); `balance`
        only prices the native leg, so a USDC-funded wallet would read as empty."""
        items = self._run(["wallet", "portfolio", "--chains", self.chain, "--json"])
        h: dict[str, float] = {}
        for it in items or []:
            sym = it.get("symbol") or it.get("ticker")
            usd = it.get("usdValue")
            if sym and usd:
                h[sym] = h.get(sym, 0.0) + float(usd)
        return h

    def price_history(self, token: str, period: str = "day") -> dict:
        """{priceUsd, history:[{price,date}]} for a token on BSC."""
        return self._run(["price", token, "--chain", self.chain, "--history", period, "--json"])

    def quote(self, src: str, dst: str, usd: float, slippage: float) -> dict:
        """Quote-only swap of `usd` worth of src->dst. Tokens passed as contracts/symbols."""
        return self._run([
            "swap", src, dst, "--usd", f"{usd:.6f}", "--chain", self.chain,
            "--slippage", f"{slippage * 100:.4f}", "--quote-only", "--json",
        ])

    def sellable(self, contract: str, usd: float = 25.0, slippage: float = 0.05) -> bool:
        """Honeypot/liquidity gate: a token must quote a SELL back to the settlement stable to be tradeable."""
        try:
            q = self.quote(contract, SETTLEMENT, usd, slippage)
        except TwakError:
            return False
        return bool(q.get("output")) and float(q.get("priceImpact", 0) or 0) < slippage * 100

    def risk_clean(self, asset_id: str) -> bool | None:
        """Soft Trust Wallet risk check. None when the API is unavailable (403/key tier)."""
        try:
            r = self._run(["risk", asset_id, "--json"])
        except TwakError:
            return None
        if r.get("errorCode") in ("NETWORK_ERROR",) or "403" in str(r.get("error", "")):
            return None
        if r.get("errorCode") == "TOKEN_NOT_FOUND":
            return None
        flagged = r.get("isMalicious") or r.get("isScam") or r.get("honeypot")
        return not bool(flagged)

    # --- write (guarded) ---
    def swap(self, src: str, dst: str, usd: float, slippage: float,
             password: str | None = None) -> dict:
        """Execute a swap. Refuses unless quote_only is explicitly disabled (live arm)."""
        if self.quote_only:
            raise TwakError("swap() called while quote_only=True — dry-run guard active")
        args = ["swap", src, dst, "--usd", f"{usd:.6f}", "--chain", self.chain,
                "--slippage", f"{slippage * 100:.4f}", "--json"]
        if password:
            args += ["--password", password]
        return self._run(args)
