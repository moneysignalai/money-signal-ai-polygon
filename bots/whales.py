# bots/whales.py — Whale options flow bot (CALL + PUT, big notional orders)
#
# Hunts for:
#   • Large single-option orders (CALL or PUT)
#   • Uses Polygon option-chain + last-trade cache from shared.py
#   • Focused on big notional (defaults: $500k+) and decent size
#
# One alert per contract per day.

import os
from datetime import datetime, date

import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
    now_est,  # returns string from shared.py
)

eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG (tunable via ENV) ----------------

# If you want more hits, lower these via env:
#   WHALES_MIN_NOTIONAL=300000
#   WHALES_MIN_SIZE=25
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


# ---------------- HELPERS ----------------

def _safe_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_int(x):
    try:
        if x is None:
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _format_now_est() -> str:
    """
    Make a nice EST timestamp, regardless of whether shared.now_est()
    returns a datetime or a string.
    """
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def _underlying_price_from_opt(opt: dict) -> float | None:
    """
    For Polygon v3 snapshot /options:
      opt["underlying_asset"]["price"]
    """
    try:
        ua = opt.get("underlying_asset") or {}
        val = ua.get("price")
        return float(val) if val is not None else None
    except Exception:
        return None


def _parse_option_symbol(sym: str):
    """
    Robust parser for Polygon option symbol, e.g.:

      O:TSLA251121C00450000

    Underlying: TSLA
    Expiry: 2025-11-21
    Call/Put: C or P
    Strike: 450.00
    """
    if not sym or not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]

        # find first digit (start of YYMMDD) — safer than base.find("2")
        idx = 0
        while idx < len(base) and not base[idx].isdigit():
            idx += 1

        under = base[:idx]
        rest = base[idx:]

        if len(rest) < 7:
            return None, None, None, None

        exp_raw = rest[:6]      # YYMMDD
        cp_char = rest[6]       # C/P
        strike_raw = rest[7:]   # strike * 1000

        yy = int("20" + exp_raw[0:2])
        mm = int(exp_raw[2:4])
        dd = int(exp_raw[4:6])
        expiry = datetime(yy, mm, dd).date()

        strike = int(strike_raw) / 1000.0 if strike_raw else None

        return under, expiry, cp_char, strike
    except Exception:
        return None, None, None, None


def _days_to_expiry(expiry) -> int | None:
    if not expiry:
        return None
    today = date.today()
    return (expiry - today).days


def _resolve_whale_universe():
    """
    Universe priority:
      1) WHALES_TICKER_UNIVERSE env
      2) Dynamic top-volume universe (shared)
      3) TICKER_UNIVERSE env (global)
      4) Hard-coded top 100 liquid tickers (final fallback)
    """
    env = os.getenv("WHALES_TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]

    uni = get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.92)
    if uni:
        return uni

    env2 = os.getenv("TICKER_UNIVERSE")
    if env2:
        return [t.strip().upper() for t in env2.split(",") if t.strip()]

    # Last resort: broad, liquid list
    return [
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


# ---------------- MAIN BOT ----------------

async def run_whales():
    """
    Whale options flow scanner.

    • Scans a liquid universe.
    • Looks for single big CALL/PUT trades:
        - size >= MIN_WHALE_SIZE
        - notional >= MIN_WHALE_NOTIONAL
        - DTE between 0 and MAX_WHALE_DTE
    • One alert per contract per day.
    """
    _reset_day()

    universe = _resolve_whale_universe()
    if not universe:
        print("[whales] empty universe; skipping.")
        return

    time_str = _format_now_est()

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        # v3 snapshot has "results" (list). Some older responses might use "result".
        opts = chain.get("results") or chain.get("result") or []
        if not isinstance(opts, list) or not opts:
            continue

        for opt in opts:
            details = opt.get("details") or {}

            # Full option ticker, e.g., "O:TSLA251121C00450000"
            contract = details.get("ticker") or opt.get("ticker")
            if not contract or _already_alerted(contract):
                continue

            # Last trade for this contract (v3 /last/trade)
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            t_res = trade.get("results") or {}
            if isinstance(t_res, list):
                if not t_res:
                    continue
                last = t_res[0]
            elif isinstance(t_res, dict):
                last = t_res
            else:
                continue

            price = _safe_float(last.get("p") or last.get("price"))
            size = _safe_int(last.get("s") or last.get("size"))

            if price is None or size is None:
                continue
            if price <= 0 or size <= 0:
                continue
            if size < MIN_WHALE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_WHALE_NOTIONAL:
                continue

            # Parse option symbol → underlying, expiry, C/P, strike
            under, expiry, cp_raw, _strike = _parse_option_symbol(contract)

            # Fallback expiry from snapshot details if symbol parse failed
            if not expiry:
                exp_str = details.get("expiration_date")
                if exp_str:
                    try:
                        expiry = datetime.strptime(exp_str, "%Y-%m-%d").date()
                    except Exception:
                        expiry = None

            if not under:
                under = sym

            dte = _days_to_expiry(expiry) if expiry else None
            if dte is None or dte < 0 or dte > MAX_WHALE_DTE:
                continue

            # Determine CALL / PUT
            cp = None
            if cp_raw:
                cp = "CALL" if cp_raw.upper() == "C" else "PUT"
            else:
                ct = (details.get("contract_type") or "").upper()
                if ct in ("CALL", "PUT"):
                    cp = ct

            cp_label = cp or "Option"

            under_px = _underlying_price_from_opt(opt)
            if under_px is not None:
                header_price_line = f"💰 Underlying ${under_px:.2f}"
            else:
                header_price_line = "💰 Underlying price N/A"

            extra = (
                f"🐋 WHALES — {under}\n"
                f"🕒 {time_str}\n"
                f"{header_price_line}\n"
                "────────────\n"
                f"🐋 Large {cp_label} order detected\n"
                f"📌 Contract: {contract}\n"
                f"💵 Option Price: ${price:.2f}\n"
                f"📦 Size: {size:,} · Notional: ≈ ${notional:,.0f}\n"
                f"🗓️ DTE: {dte}\n"
                f"🔗 Chart: {chart_link(under)}"
            )

            # rvol not relevant here → pass 0.0
            send_alert("whales", under, price, 0.0, extra=extra)
            _mark(contract)

    print("[whales] scan complete.")