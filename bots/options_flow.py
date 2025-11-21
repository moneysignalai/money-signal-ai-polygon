# bots/options_flow.py
#
# Unified options flow scanner:
#   – CHEAP lottos
#   – UNUSUAL sweeps
#   – WHALE-sized orders
#
# Reads env thresholds from the same vars you already use:
#   CHEAP_* , UNUSUAL_* , WHALES_*
# So you don't need to change your environment.

import os
from datetime import datetime, date

import pytz

from bots.shared import (
    POLYGON_KEY,
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
    now_est,
    is_etf_blacklisted,
    minutes_since_midnight_est,
)

eastern = pytz.timezone("US/Eastern")

# ---------------- TIME WINDOW (RTH) ----------------

RTH_START_MIN = 9 * 60 + 30   # 09:30
RTH_END_MIN   = 16 * 60       # 16:00


def _in_rth_window() -> bool:
    mins = minutes_since_midnight_est()
    return RTH_START_MIN <= mins <= RTH_END_MIN


# ---------------- CONFIG (reuses your env vars) ----------------

# Cheap lottos
CHEAP_MAX_PREMIUM   = float(os.getenv("CHEAP_MAX_PREMIUM", "0.35"))
CHEAP_MIN_SIZE      = int(os.getenv("CHEAP_MIN_SIZE", "100"))
CHEAP_MIN_NOTIONAL  = float(os.getenv("CHEAP_MIN_NOTIONAL", "10000"))

# Unusual sweeps
UNUSUAL_MIN_NOTION  = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "100000"))
UNUSUAL_MIN_SIZE    = int(os.getenv("UNUSUAL_MIN_SIZE", "10"))
UNUSUAL_MAX_DTE     = int(os.getenv("UNUSUAL_MAX_DTE", "45"))

# Whale flows
WHALES_MIN_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "500000"))
WHALES_MIN_SIZE     = int(os.getenv("WHALES_MIN_SIZE", "50"))
WHALES_MAX_DTE      = int(os.getenv("WHALES_MAX_DTE", "90"))

# Universe size
MAX_UNIVERSE = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", "120"))

# Per-day de-dupe (per category, per contract)
_alert_date: date | None = None
_seen_cheap: set[str] = set()
_seen_unusual: set[str] = set()
_seen_whale: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _seen_cheap, _seen_unusual, _seen_whale
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _seen_cheap = set()
        _seen_unusual = set()
        _seen_whale = set()


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
        expiry = date(yy, mm, dd)

        strike = int(strike_raw) / 1000.0 if strike_raw else None

        return under, expiry, cp_char, strike
    except Exception:
        return None, None, None, None


def _days_to_expiry(expiry: date | None) -> int | None:
    if not expiry:
        return None
    today = date.today()
    return (expiry - today).days


def _underlying_price_from_opt(opt: dict) -> float | None:
    try:
        ua = opt.get("underlying_asset") or {}
        val = ua.get("price")
        return float(val) if val is not None else None
    except Exception:
        return None


def _contract_type(opt: dict, cp_raw: str | None) -> str | None:
    """Return 'CALL' or 'PUT' if we can figure it out."""
    if cp_raw:
        return "CALL" if cp_raw.upper() == "C" else "PUT"
    details = opt.get("details") or {}
    ct = (details.get("contract_type") or "").upper()
    if ct in ("CALL", "PUT"):
        return ct
    return None


def _format_time() -> str:
    """
    now_est() from shared returns a human-friendly string already.
    We keep this wrapper in case we ever change shared.now_est.
    """
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def _resolve_universe() -> list[str]:
    # Allow a dedicated override
    env = os.getenv("OPTIONS_FLOW_TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]

    # else fall back to your normal dynamic + global TICKER_UNIVERSE
    env2 = os.getenv("TICKER_UNIVERSE")
    if env2:
        return [t.strip().upper() for t in env2.split(",") if t.strip()]

    return get_dynamic_top_volume_universe(max_tickers=MAX_UNIVERSE, volume_coverage=0.90)


# ---------------- MAIN BOT ----------------

async def run_options_flow():
    """
    Unified options flow scanner.

    For each symbol in universe:
      1. Fetch option chain once via get_option_chain_cached().
      2. For each option:
           - Fetch last trade via get_last_option_trades_cached().
           - Compute price, size, notional, DTE, contract type, underlying price.
           - Classify into ONE of:
               a) WHALE (highest priority)
               b) UNUSUAL
               c) CHEAP
           - Fire a single alert per contract per category per day.
    """
    if not POLYGON_KEY:
        print("[options_flow] POLYGON_KEY missing; skipping.")
        return

    if not _in_rth_window():
        print("[options_flow] outside RTH; skipping.")
        return

    _reset_if_new_day()

    universe = _resolve_universe()
    if not universe:
        print("[options_flow] empty universe; skipping.")
        return

    print(f"[options_flow] scanning {len(universe)} symbols")

    time_str = _format_time()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        opts = chain.get("results") or chain.get("result") or chain.get("options") or []
        if not isinstance(opts, list) or not opts:
            continue

        for opt in opts:
            details = opt.get("details") or {}
            contract = details.get("ticker") or opt.get("ticker")
            if not contract:
                continue

            # Last trade for this contract
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

            notional = price * size * 100.0

            # Parse symbol & expiry
            under, expiry, cp_raw, _strike = _parse_option_symbol(contract)
            if not under:
                under = sym

            dte = _days_to_expiry(expiry)
            if dte is None or dte < 0:
                continue

            cp = _contract_type(opt, cp_raw)
            under_px = _underlying_price_from_opt(opt)

            # --- CATEGORY DECISION (priority: WHALE > UNUSUAL > CHEAP) ---

            category = None

            # WHALE
            if (
                dte <= WHALES_MAX_DTE
                and size >= WHALES_MIN_SIZE
                and notional >= WHALES_MIN_NOTIONAL
                and contract not in _seen_whale
            ):
                category = "whale"

            # UNUSUAL (if not already whale)
            elif (
                dte <= UNUSUAL_MAX_DTE
                and size >= UNUSUAL_MIN_SIZE
                and notional >= UNUSUAL_MIN_NOTION
                and contract not in _seen_unusual
            ):
                category = "unusual"

            # CHEAP (CALL only, low premium)
            elif (
                cp == "CALL"
                and price <= CHEAP_MAX_PREMIUM
                and size >= CHEAP_MIN_SIZE
                and notional >= CHEAP_MIN_NOTIONAL
                and contract not in _seen_cheap
            ):
                category = "cheap"

            if not category:
                continue

            # ---------------- ALERT FORMATTING ----------------

            notional_rounded = round(notional)
            dte_str = f"{dte} days" if dte is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P" if cp == "PUT" else "?"

            # Base contract line
            contract_line = f"{under} {expiry.strftime('%b %d %Y') if expiry else 'N/A'} {cp_letter}"

            # Build category-specific header & description
            if category == "whale":
                header = f"🐋 WHALES — {sym}"
                desc = "🐋 Large {side} order detected".format(
                    side=(cp or "Option")
                )
            elif category == "unusual":
                header = f"🕵️ UNUSUAL — {sym}"
                desc = f"🕵️ Unusual {(cp or 'Option')} sweep detected"
            else:  # cheap
                header = f"🧨 CHEAP — {sym}"
                desc = "🎯 Cheap CALL lotto flow"

            # Underlying line
            if under_px is not None:
                under_line = f"💰 Underlying ${under_px:.2f}"
            else:
                under_line = "💰 Underlying price N/A"

            extra_lines = [
                header,
                f"🕒 {time_str}",
                under_line,
                "────────────",
                desc,
                f"📌 Contract: {contract}",
                f"📦 Size: {size:,}",
                f"💵 Option Price: ${price:.2f}",
                f"💰 Notional: ≈ ${notional_rounded:,.0f}",
                f"🗓️ DTE: {dte_str}",
                f"🔗 Chart: {chart_link(sym)}",
            ]

            extra_text = "\n".join(extra_lines)

            # rvol unknown → 0.0, we care about body text
            send_alert("options_flow", sym, price, 0.0, extra=extra_text)

            # Mark as seen for that category
            if category == "whale":
                _seen_whale.add(contract)
            elif category == "unusual":
                _seen_unusual.add(contract)
            else:
                _seen_cheap.add(contract)

    print("[options_flow] scan complete.")