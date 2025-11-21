# bots/unusual.py — premium-style unusual options sweeps (CALL + PUT)

import os
from datetime import datetime, date

import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,  # cached v3 /last/trade helper
    send_alert,
    chart_link,
    now_est,  # may be str OR datetime depending on shared.py
)

eastern = pytz.timezone("US/Eastern")

# ---------------- ENV CONFIG (tunable) ----------------

# Minimum notional (price * size * 100) per sweep
MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "100000"))

# Minimum number of contracts in the last trade
MIN_TRADE_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "10"))

# Maximum days to expiration
MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))

# --------------- Per-day dedupe (per contract) ----------------

alert_date: date | None = None
alerted_contracts: set[str] = set()


def _reset_day() -> None:
    """Reset daily state if we rolled to a new calendar day."""
    global alert_date, alerted_contracts
    today = date.today()
    if alert_date != today:
        alert_date = today
        alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    return contract in alerted_contracts


def _mark(contract: str) -> None:
    alerted_contracts.add(contract)


# --------------- Helpers to parse option symbols ----------------

def _parse_option_symbol(sym: str):
    """
    Polygon option symbol example: O:TSLA251121C00450000

    Underlying: TSLA
    Expiry: 2025-11-21
    Call/Put: C or P
    Strike: 450.00
    """
    if not sym or not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]

        # find first digit (start of YYMMDD)
        idx = 0
        while idx < len(base) and not base[idx].isdigit():
            idx += 1

        under = base[:idx]
        rest = base[idx:]

        if len(rest) < 7:
            return None, None, None, None

        exp_raw = rest[:6]      # YYMMDD
        cp_char = rest[6]       # C/P
        strike_raw = rest[7:]   # 000450000

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


def _strike_from_opt(opt: dict) -> float | None:
    """
    For Polygon v3 snapshot /options:
      opt["details"]["strike_price"]
    """
    try:
        details = opt.get("details") or {}
        val = details.get("strike_price")
        return float(val) if val is not None else None
    except Exception:
        return None


def _moneyness_label(under_px: float | None, strike: float | None, cp: str | None):
    """
    Return (label, pct_distance) where label in {ITM, ATM, OTM, N/A}
    and pct_distance is |(strike-under)/under| * 100.
    """
    if under_px is None or strike is None or under_px <= 0:
        return "N/A", 0.0

    dist_pct = abs(strike - under_px) / under_px * 100.0

    if cp == "CALL":
        if strike < under_px:
            label = "ITM"
        elif dist_pct <= 1.0:
            label = "ATM"
        else:
            label = "OTM"
    elif cp == "PUT":
        if strike > under_px:
            label = "ITM"
        elif dist_pct <= 1.0:
            label = "ATM"
        else:
            label = "OTM"
    else:
        label = "N/A"

    return label, dist_pct


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


# --------------- Core scan ----------------

async def run_unusual():
    """
    Scan a liquid universe for large, unusual single-sweep options trades.
    """
    _reset_day()

    # Primary source: dynamic universe from Polygon
    universe = get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.90)

    # Fallbacks if dynamic universe is empty
    if not universe:
        env = os.getenv("TICKER_UNIVERSE")
        if env:
            universe = [x.strip().upper() for x in env.split(",") if x.strip()]
        else:
            universe = [
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

    if not universe:
        print("[unusual] empty universe even after fallback; skipping.")
        return

    time_str = _format_now_est()

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        # v3 snapshot options: main payload usually under "results"
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
            if size < MIN_TRADE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_NOTIONAL:
                continue

            # Parse symbol first, then fall back to details
            under, expiry, cp_raw, strike_from_sym = _parse_option_symbol(contract)

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
            if dte is None or dte < 0 or dte > MAX_DTE:
                continue

            # Determine CALL / PUT
            cp = None
            if cp_raw:
                cp = "CALL" if cp_raw.upper() == "C" else "PUT"
            else:
                ct = (details.get("contract_type") or "").upper()
                if ct in ("CALL", "PUT"):
                    cp = ct

            under_px = _underlying_price_from_opt(opt)
            strike = strike_from_sym if strike_from_sym is not None else _strike_from_opt(opt)
            m_label, m_dist = _moneyness_label(under_px, strike, cp)

            if expiry:
                exp_fmt = expiry.strftime("%b %d %Y")
            else:
                exp_fmt = "N/A"

            strike_str = f"{strike:.2f}" if strike is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P" if cp == "PUT" else "?"

            contract_line = f"{sym} {exp_fmt} {strike_str} {cp_letter}"

            if m_label == "N/A":
                moneyness_text = "Moneyness N/A"
            else:
                moneyness_text = f"{m_label} · Moneyness {m_dist:.1f}%"

            if under_px is not None:
                header_price_line = f"💰 Underlying ${under_px:.2f}"
            else:
                header_price_line = "💰 Underlying price N/A"

            extra = (
                f"🕵️ UNUSUAL — {sym}\n"
                f"🕒 {time_str}\n"
                f"{header_price_line}\n"
                "────────────\n"
                f"🕵️ Unusual {cp or 'Option'} sweep: {contract_line}\n"
                f"📌 Flow Type: Single large {(cp or 'option').lower()} sweep\n"
                f"⏱ DTE: {dte} · {moneyness_text}\n"
                f"📦 Volume: {size:,} · Avg: ${price:.2f}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("unusual", sym, price, 0.0, extra=extra)
            _mark(contract)