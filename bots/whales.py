# bots/whales.py — Whale options flow bot (CALL + PUT, $500k+ defaults)
#
# Hunts for:
#   • Large single-option orders (CALL or PUT)
#   • Uses Polygon option-chain + last-trade cache from shared.py
#   • Focused on big notional (defaults: $500k+) and decent size
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
MIN_WHALE_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "500000"))  # default $500k+

# Minimum size in contracts
MIN_WHALE_SIZE = int(os.getenv("WHALES_MIN_SIZE", "50"))  # default 50 contracts

# Maximum DTE to keep focus on nearer-term flow
MAX_WHALE_DTE = int(os.getenv("WHALES_MAX_DTE", "90"))  # default up to ~3 months

alert_date: date | None = None
alerted_contracts: set[str] = set()


def _reset_day() -> None:
    global alert_date, alerted_contracts
    today = date.today()
    if alert_date != today:
        alert_date = today
        alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    return contract in alerted_contracts


def _mark(contract: str) -> None:
    alerted_contracts.add(contract)


def _parse_option_symbol(sym: str):
    if not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]
        under = base[: base.find("2")]
        rest = base[len(under):]

        exp_raw = rest[:6]      # YYMMDD
        cp = rest[6]            # C/P
        strike_raw = rest[7:]   # 000450000

        yy = int("20" + exp_raw[0:2])
        mm = int(exp_raw[2:4])
        dd = int(exp_raw[4:6])
        expiry = datetime(yy, mm, dd).date()

        strike = int(strike_raw) / 1000.0

        return under, expiry, cp, strike
    except Exception:
        return None, None, None, None


def _days_to_expiry(expiry) -> int | None:
    if not expiry:
        return None
    today = date.today()
    return (expiry - today).days


async def run_whales():
    """
    Look for very large, single-option whale orders.
    """
    _reset_day()

    universe = get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.95)
    if not universe:
        print("[whales] empty universe; skipping.")
        return

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        opts = chain.get("result") or chain.get("results") or []
        if not opts:
            continue

        for opt in opts:
            contract = opt.get("ticker")
            if not contract or _already_alerted(contract):
                continue

            last_trade = get_last_option_trades_cached(contract)
            if not last_trade:
                continue

            try:
                last = last_trade.get("results", [{}])[0]
            except Exception:
                continue

            price = last.get("p")
            size = last.get("s")
            if price is None or size is None:
                continue

            try:
                price = float(price)
                size = int(size)
            except Exception:
                continue

            if price <= 0:
                continue
            if size < MIN_WHALE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_WHALE_NOTIONAL:
                continue

            under, expiry, cp_raw, _ = _parse_option_symbol(contract)
            if not under or not expiry or not cp_raw:
                continue

            dte = _days_to_expiry(expiry)
            if dte is None or dte < 0 or dte > MAX_WHALE_DTE:
                continue

            cp = "CALL" if cp_raw.upper() == "C" else "PUT"

            # Nicely formatted EST timestamp
            time_str = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

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