# bots/whales.py — Whale options flow bot (CALL + PUT, $1M+ defaults)
#
# Hunts for:
#   • Large single-option orders (CALL or PUT)
#   • Uses Polygon option-chain + last-trade cache from shared.py
#   • Focused on big notional (defaults: $1M+) and decent size
#
# One alert per contract per day, formatted in premium Telegram style.

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

# ---------------- CONFIG (tunable via ENV) ----------------

# Minimum notional for a whale order (price * size * 100)
MIN_WHALE_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "1000000"))  # default $1M+

# Minimum contracts in the print
MIN_WHALE_SIZE = int(os.getenv("WHALES_MIN_SIZE", "100"))  # default 100 contracts

# Maximum DTE to keep focus on nearer-term flow
MAX_WHALE_DTE = int(os.getenv("WHALES_MAX_DTE", "120"))  # default up to ~6 months

# Per-day de-duplication by contract symbol
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


def _in_rth() -> bool:
    """Regular trading hours: 09:30–16:00 ET, Mon–Fri."""
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def _calc_dte(expiration: str | None, today: date) -> int | None:
    if not expiration:
        return None
    try:
        exp_d = datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp_d - today).days
    except Exception:
        return None


async def run_whales():
    """Whale Flow Bot.

    Logic:
      • Only scans during RTH (09:30–16:00 ET, Mon–Fri).
      • Universe: dynamic top-volume tickers (or TICKER_UNIVERSE if set).
      • For each underlying:
          - Fetch option chain via get_option_chain_cached(sym).
          - For each contract:
              • CALL or PUT
              • 0 <= DTE <= MAX_WHALE_DTE
              • Last trade exists via get_last_option_trades_cached(contract)
              • size >= MIN_WHALE_SIZE
              • notional >= MIN_WHALE_NOTIONAL
          - Per-contract per-day de-dupe.
    """
    if not _in_rth():
        return

    universe = get_dynamic_top_volume_universe()
    if not universe:
        print("[whales] Universe empty; skipping.")
        return

    today = date.today()

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

            if _already(contract):
                continue

            cp_raw = details.get("contract_type")
            if cp_raw not in ("call", "put"):
                continue
            cp = "CALL" if cp_raw == "call" else "PUT"

            dte = _calc_dte(details.get("expiration_date"), today)
            if dte is None or dte < 0 or dte > MAX_WHALE_DTE:
                continue

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
            if size < MIN_WHALE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_WHALE_NOTIONAL:
                continue

            # Format alert
            time_str = now_est()

            extra = (
                f"🐋 WHALES — {sym}\n"
                f"🕒 {time_str}\n"
                "────────────\n"
                f"🐋 Large {cp} order detected\n"
                f"📌 Contract: `{contract}`\n"
                f"💵 Option Price: ${price:.2f}\n"
                f"📦 Size: {size:,} · Notional: ≈ ${notional:,.0f}\n"
                f"🗓️ DTE: {dte}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("whales", sym, price, 0, extra=extra)
            _mark(contract)