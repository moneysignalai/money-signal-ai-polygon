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

MIN_WHALE_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "500000"))  # default $500k+
MIN_WHALE_SIZE = int(os.getenv("WHALES_MIN_SIZE", "50"))
MAX_WHALE_DTE = int(os.getenv("WHALES_MAX_DTE", "90"))

alert_date: date | None = None
alerted_contracts: set[str] = set()


# ---------------- STATE MGMT ----------------

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


# ---------------- UNIVERSE RESOLUTION ----------------

def _resolve_whale_universe():
    """
    Universe priority:
      1) WHALES_TICKER_UNIVERSE env
      2) Dynamic top-volume universe (shared)
      3) TICKER_UNIVERSE env (global)
      4) Hard-coded top 100 liquid tickers (final fallback)
    """
    # 1) Specific override for whales
    env = os.getenv("WHALES_TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]

    # 2) Primary: dynamic universe
    uni = get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.92)

    # 3) If dynamic universe broke → global environment
    if not uni:
        env2 = os.getenv("TICKER_UNIVERSE")
        if env2:
            uni = [t.strip().upper() for t in env2.split(",") if t.strip()]
        else:
            # 4) Last resort: top 100
            uni = [
                "SPY","QQQ","IWM","DIA","VTI",
                "XLK","XLF","XLE","XLY","XLI",
                "AAPL","MSFT","NVDA","TSLA","META",
                "GOOGL","AMZN","NFLX","AVGO","ADBE",
                "SMCI","AMD","INTC","MU","ORCL",
                "CRM","SHOP","PANW","ARM","CSCO",
                "PLTR","SOFI","SNOW","UBER","LYFT",
                "ABNB","COIN","HOOD","RIVN","LCID",
                "NIO","F","GM","T","VZ",
                "BAC","JPM","WFC","C","GS",
                "XOM","CVX","OXY","SLB","COP",
                "PFE","MRK","LLY","UNH","ABBV",
                "TSM","BABA","JD","NKE","MCD",
                "SBUX","WMT","COST","HD","LOW",
                "DIS","PARA","WBD","TGT","SQ",
                "PYPL","ROKU","ETSY","NOW","INTU",
                "TXN","QCOM","LRCX","AMAT","LIN",
                "CAT","DE","BA","LULU","GME",
                "AMC","MARA","RIOT","CLSK","BITF",
                "CIFR","HUT","BTBT","TSLY","SMH",
            ]
    return uni


# ---------------- OPTION SYMBOL PARSING ----------------

def _parse_option_symbol(sym: str):
    if not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]
        under = base[: base.find("2")]
        rest = base[len(under):]

        exp_raw = rest[:6]      # YYMMDD
        cp = rest[6]            # C/P
        strike_raw = rest[7:]

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


# ---------------- MAIN BOT ----------------

async def run_whales():
    _reset_day()

    universe = _resolve_whale_universe()
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
