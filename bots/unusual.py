# bots/unusual.py — premium-style unusual options sweeps (CALL + PUT)

import os
from datetime import datetime, date

import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")

# ---------------- ENV CONFIG (tunable) ----------------
# Override on Render if you want different aggressiveness:
#   UNUSUAL_MIN_NOTIONAL, UNUSUAL_MIN_SIZE, UNUSUAL_MAX_DTE

# Minimum notional (price * size * 100) per sweep
MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "75000"))   # default $75k+

# Minimum number of contracts in the last trade
MIN_TRADE_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))          # default 20+ contracts

# Maximum days to expiration
MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "60"))                  # default 60 days out

# --------------- Per-day dedupe (per contract) ----------------
_alert_date: date | None = None
_alerted_contracts: set[str] = set()


def _reset_day() -> None:
    """Reset daily state if we rolled to a new calendar day."""
    global _alert_date, _alerted_contracts
    today = date.today()
    if today != _alert_date:
        _alert_date = today
        _alerted_contracts = set()


def _already(contract: str) -> bool:
    """Check if this contract was already alerted today."""
    _reset_day()
    return contract in _alerted_contracts


def _mark(contract: str) -> None:
    """Mark a contract as alerted for the current day."""
    _reset_day()
    _alerted_contracts.add(contract)


# ---------------- TIME WINDOW (RTH ONLY) ----------------
def _in_rth() -> bool:
    """
    Only scan during regular trading hours:
      09:30–16:00 ET, Mon–Fri.
    """
    now = datetime.now(eastern)
    if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
        return False

    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


# ---------------- HELPERS ----------------
def _calc_dte(expiration: str | None, today: date) -> int | None:
    """Compute days-to-expiration from YYYY-MM-DD."""
    if not expiration:
        return None
    try:
        exp_d = datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp_d - today).days
    except Exception:
        return None


def _format_exp(expiration: str | None) -> str:
    """
    Turn '2025-11-21' into '11/21/2025' for nice display.
    """
    if not expiration:
        return "N/A"
    try:
        dt = datetime.strptime(expiration, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return expiration


def _underlying_price_from_opt(opt: dict) -> float | None:
    """
    Best-effort extraction of underlying last price from the option snapshot.
    """
    try:
        ua = opt.get("underlying_asset") or {}
        cand = (
            ua.get("price")
            or ua.get("underlying_price")
            or opt.get("underlying_price")
        )
        if cand is None:
            return None
        px = float(cand)
        return px if px > 0 else None
    except Exception:
        return None


def _strike_from_opt(opt: dict) -> float | None:
    try:
        details = opt.get("details") or {}
        strike = details.get("strike_price") or opt.get("strike_price")
        if strike is None:
            return None
        s = float(strike)
        return s if s > 0 else None
    except Exception:
        return None


def _moneyness_label(under_px: float | None, strike: float | None, cp: str) -> tuple[str, float]:
    """
    Returns (label, distance_pct) where label ∈ {"ATM", "ITM", "OTM", "N/A"}.
    Distance is |strike - underlying| / underlying * 100.
    """
    if under_px is None or strike is None or under_px <= 0:
        return "N/A", 0.0

    dist_pct = abs(strike - under_px) / under_px * 100.0

    # Very close = ATM
    if dist_pct < 1.0:
        return "ATM", dist_pct

    # Simple ITM/OTM logic:
    # For CALLS: strike < under_px -> ITM
    # For PUTS:  strike > under_px -> ITM
    if cp == "CALL":
        label = "ITM" if strike < under_px else "OTM"
    else:  # PUT
        label = "ITM" if strike > under_px else "OTM"

    return label, dist_pct


# ---------------- MAIN BOT ----------------
async def run_unusual():
    """
    Unusual Options Flow Bot (CALL + PUT):

      • Time: RTH only (09:30–16:00 ET, Mon–Fri).
      • Universe: dynamic top-volume tickers (or TICKER_UNIVERSE if set).
      • For each underlying:
          - Fetch option chain via get_option_chain_cached(sym).
          - For each contract:
              • CALL or PUT
              • 0 <= DTE <= MAX_DTE
              • Last trade exists via get_last_option_trades_cached(contract)
              • size >= MIN_TRADE_SIZE
              • notional (price * size * 100) >= MIN_NOTIONAL
          - Per-contract per-day: only 1 alert.

      Alert format (similar to your original NOK alert):

        🕵️ UNUSUAL — SYM
        🕒 03:57 PM EST · Nov 19
        💰 $UNDERLYING
        ────────────
        🕵️ Unusual CALL sweep: SYM 11/21/2025 6.00 C
        📌 Flow Type: Single large CALL sweep
        ⏱ DTE: 2 · ITM · Moneyness 0.5%
        📦 Volume: 13,401 · Avg: $0.16
        💰 Notional: ≈ $213,428
        🔗 Chart: ...
    """
    if not _in_rth():
        # keep logs quiet, but you can uncomment if you want
        # print("[unusual] Outside RTH; skipping.")
        return

    today = date.today()

    # Expanded universe to see more symbols
    universe = get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)
    if not universe:
        print("[unusual] Universe empty; skipping.")
        return

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        results = chain.get("results") or chain.get("options") or []
        if not results:
            continue

        for opt in results:
            details = opt.get("details") or {}

            # Resolve contract symbol (Polygon can use different keys)
            contract = (
                opt.get("ticker")
                or opt.get("option_symbol")
                or details.get("symbol")
                or details.get("ticker")
            )
            if not contract:
                continue
            contract = str(contract)

            # Per-contract dedupe
            if _already(contract):
                continue

            # CALL or PUT
            cp_raw = details.get("contract_type")
            if cp_raw not in ("call", "put"):
                continue
            cp = "CALL" if cp_raw == "call" else "PUT"

            # DTE + expiration formatting
            expiration = details.get("expiration_date")
            dte = _calc_dte(expiration, today)
            if dte is None or dte < 0 or dte > MAX_DTE:
                continue
            exp_fmt = _format_exp(expiration)

            # Last trade from cached helper
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            last = trade.get("results") or {}

            # Polygon last-trade fields for options:
            #   p = price, s = size
            price = last.get("p")
            size = last.get("s", 0)

            try:
                price = float(price) if price is not None else None
                size = int(size)
            except Exception:
                continue

            if price is None or price <= 0:
                continue
            if size < MIN_TRADE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_NOTIONAL:
                continue

            # Underlying & strike & moneyness
            under_px = _underlying_price_from_opt(opt)
            strike = _strike_from_opt(opt)
            m_label, m_dist = _moneyness_label(under_px, strike, cp)

            # Build nice contract line: SYM 11/21/2025 6.00 C
            strike_str = f"{strike:.2f}" if strike is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P"
            contract_line = f"{sym} {exp_fmt} {strike_str} {cp_letter}"

            # Human friendly moneyness text
            if m_label == "N/A":
                moneyness_text = "Moneyness N/A"
            else:
                moneyness_text = f"{m_label} · Moneyness {m_dist:.1f}%"

            # Format header underlying price
            if under_px is not None:
                header_price_line = f"💰 ${under_px:.2f}"
            else:
                header_price_line = "💰 (underlying N/A)"

            time_str = now_est()

            extra = (
                f"🕵️ UNUSUAL — {sym}\n"
                f"🕒 {time_str}\n"
                f"{header_price_line}\n"
                "────────────\n"
                f"🕵️ Unusual {cp} sweep: {contract_line}\n"
                f"📌 Flow Type: Single large {cp.lower()} sweep\n"
                f"⏱ DTE: {dte} · {moneyness_text}\n"
                f"📦 Volume: {size:,} · Avg: ${price:.2f}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("unusual", sym, price, 0, extra=extra)
            _mark(contract)