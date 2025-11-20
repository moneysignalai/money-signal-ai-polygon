# bots/unusual.py — premium-style unusual options sweeps (CALL + PUT)

import os
from datetime import datetime, date

import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,  # uses shared cached last-trade helper
    send_alert,
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")

# ---------------- ENV CONFIG (tunable) ----------------
# Override on Render if you want different aggressiveness:
#   UNUSUAL_MIN_NOTIONAL, UNUSUAL_MIN_SIZE, UNUSUAL_MAX_DTE

# Minimum notional (price * size * 100) per sweep
# 🔥 Raise floor to $100k by default
MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "100000"))  # default $100k+

# Minimum number of contracts in the last trade
MIN_TRADE_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "10"))          # default 10+ contracts

# Maximum days to expiration
MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))                  # default 45 days out

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
    Polygon option symbol example:

    O:TSLA251121C00450000

    Underlying: TSLA
    Expiry: 2025-11-21
    Call/Put: C or P
    Strike: 450.00
    """
    if not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]

        # Find first digit – start of YYMMDD
        idx = 0
        while idx < len(base) and not base[idx].isdigit():
            idx += 1

        under = base[:idx]
        rest = base[idx:]

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


def _underlying_price_from_opt(opt: dict) -> float | None:
    try:
        return float(opt.get("underlying_price"))
    except Exception:
        return None


def _strike_from_opt(opt: dict) -> float | None:
    try:
        return float(opt.get("strike_price"))
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


def _extract_price_and_size(last_trade: dict):
    """
    Make this robust to different Polygon shapes.

    Expected possibilities:
      • {"results": {...}}               # dict
      • {"results": [{...}, ...]}        # list
      • {"last": {...}}                  # some endpoints
    """
    if not last_trade:
        return None, None

    trade = None

    if "results" in last_trade:
        res = last_trade["results"]
        if isinstance(res, list) and res:
            trade = res[0]
        elif isinstance(res, dict):
            trade = res
    elif "last" in last_trade:
        trade = last_trade["last"]

    if trade is None:
        trade = last_trade

    price = (
        trade.get("p")
        or trade.get("price")
        or trade.get("P")
    )
    size = (
        trade.get("s")
        or trade.get("size")
        or trade.get("q")
        or trade.get("volume")
    )

    try:
        price = float(price)
        size = int(size)
    except Exception:
        return None, None

    return price, size


# --------------- Core scan ----------------


async def run_unusual():
    """
    Scan the dynamic universe for large, unusual single-sweep options trades.
    """
    _reset_day()

    universe = get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.90)
    if not universe:
        print("[unusual] empty universe; skipping.")
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

            # 🔧 Robust extraction of price & size
            price, size = _extract_price_and_size(last_trade)
            if price is None or size is None:
                continue

            if size < MIN_TRADE_SIZE:
                continue

            notional = price * size * 100.0
            if notional < MIN_NOTIONAL:
                continue

            under, expiry, cp_raw, strike = _parse_option_symbol(contract)
            if not under or not expiry or not cp_raw:
                continue

            dte = _days_to_expiry(expiry)
            if dte is None or dte < 0 or dte > MAX_DTE:
                continue

            # Explicit CALL + PUT handling
            cp_raw_up = cp_raw.upper()
            if cp_raw_up == "C":
                cp = "CALL"
            elif cp_raw_up == "P":
                cp = "PUT"
            else:
                continue  # unknown type, skip

            under_px = _underlying_price_from_opt(opt)
            m_label, m_dist = _moneyness_label(under_px, strike, cp)

            exp_fmt = expiry.strftime("%b %d %Y")
            strike_str = f"{strike:.2f}" if strike is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P"
            contract_line = f"{sym} {exp_fmt} {strike_str} {cp_letter}"

            if m_label == "N/A":
                moneyness_text = "Moneyness N/A"
            else:
                moneyness_text = f"{m_label} · Moneyness {m_dist:.1f}%"

            if under_px is not None:
                header_price_line = f"💰 Underlying ${under_px:.2f}"
            else:
                header_price_line = "💰 Underlying price N/A"

            # Nice EST timestamp
            time_str = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

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