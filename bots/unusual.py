# bots/unusual.py — premium-format unusual options sweeps (CALL + PUT)

import os
from datetime import datetime, date
import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
)

eastern = pytz.timezone("US/Eastern")

# ---------------- ENV CONFIG (LOOSER DEFAULTS) ----------------
# You can override these on Render with:
#   UNUSUAL_MIN_NOTIONAL, UNUSUAL_MIN_SIZE, UNUSUAL_MAX_DTE

# Notional per sweep (price * size * 100)
MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "75000"))   # default $75k+
# Minimum number of contracts
MIN_TRADE_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))          # default >= 20
# Maximum days to expiration
MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "60"))                  # default 60 days

# --------------- Per-day dedupe (per contract) ----------------
_alert_date: date | None = None
_alerted_contracts: set[str] = set()


def _reset_day() -> None:
    global _alert_date, _alerted_contracts
    today = date.today()
    if today != _alert_date:
        _alert_date = today
        _alerted_contracts = set()


def _already(contract: str) -> bool:
    _reset_day()
    return contract in _alerted_contracts


def _mark(contract: str) -> None:
    _reset_day()
    _alerted_contracts.add(contract)


# ---------------- TIME WINDOW (RTH ONLY) ----------------
def _in_rth() -> bool:
    """
    Only scan during regular trading hours:
      09:30–16:00 ET, Mon–Fri.
    """
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16


# ---------------- DTE HELPER ----------------
def _calc_dte(expiration: str | None, today: date) -> int | None:
    if not expiration:
        return None
    try:
        exp_d = datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp_d - today).days
    except Exception:
        return None


# ---------------- MAIN BOT ----------------
async def run_unusual():
    """
    Unusual Options Flow Bot:

      • Time: RTH only (09:30–16:00 ET, Mon–Fri).
      • Universe: dynamic top-volume tickers (or TICKER_UNIVERSE if set).
      • For each underlying:
          - Fetch option chain via get_option_chain_cached(sym).
          - For each contract:
              • contract_type in {call, put}
              • 0 <= DTE <= MAX_DTE
              • Last trade exists via get_last_option_trades_cached(contract)
              • size >= MIN_TRADE_SIZE
              • notional (price * size * 100) >= MIN_NOTIONAL
          - Per-contract per-day: only 1 alert.
    """
    if not _in_rth():
        return

    today = date.today()
    universe = get_dynamic_top_volume_universe()
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
            contract = opt.get("ticker") or opt.get("option_symbol")
            if not contract:
                continue
            contract = str(contract)

            # Per-contract daily dedupe
            if _already(contract):
                continue

            # CALL or PUT
            cp_raw = details.get("contract_type")
            if cp_raw not in ("call", "put"):
                continue
            cp = "CALL" if cp_raw == "call" else "PUT"

            # DTE
            dte = _calc_dte(details.get("expiration_date"), today)
            if dte is None or dte < 0 or dte > MAX_DTE:
                continue

            # Last trade
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            last = trade.get("results") or {}
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

            # Format time nicely
            now = datetime.now(eastern)
            now_str = now.strftime("%I:%M %p EST · %b %d").lstrip("0")

            # Build premium alert
            msg = (
                f"🕵️ UNUSUAL — {sym}\n"
                f"🕒 {now_str}\n"
                f"💰 Trade Price: ${price:.2f}\n"
                "────────────\n"
                f"🕵️ Unusual {cp} Sweep\n"
                f"📌 Contract: `{contract}`\n"
                f"📦 Size: {size:,}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🗓️ DTE: {dte}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("unusual", sym, price, 0, extra=msg)
            _mark(contract)