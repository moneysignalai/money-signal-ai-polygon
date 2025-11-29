# bots/options_flow.py
#
# Unified options flow scanner:
#   – CHEAP lottos
#   – UNUSUAL sweeps
#   – WHALE-sized orders
#   – IV CRUSH (big IV drops on short-dated options)
#
# Reads env thresholds from the same vars you already use:
#   CHEAP_* , UNUSUAL_* , WHALES_* , IVCRUSH_*
# So you don't need to change your environment.

import os
import time
import json
import math
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import pytz

from bots.shared import (
    POLYGON_KEY,
    send_alert,
    chart_link,
    now_est,
    is_etf_blacklisted,
    minutes_since_midnight_est,
    get_option_chain_cached,
    get_last_option_trades_cached,
)
from bots.status_report import record_bot_stats  # ✅ status-report integration

eastern = pytz.timezone("US/Eastern")

# ---------------- TIME WINDOW (RTH) ----------------

RTH_START_MIN = 9 * 60 + 30   # 09:30
RTH_END_MIN   = 16 * 60       # 16:00


def _in_rth_window() -> bool:
    mins = minutes_since_midnight_est()
    return RTH_START_MIN <= mins <= RTH_END_MIN


# ---------------- CONFIG (reuses your env vars) ----------------

# Cheap lottos
# Default thresholds made a bit more permissive so you actually SEE flow
# out of the box. You can still override all of these via env vars.
CHEAP_MAX_PREMIUM   = float(os.getenv("CHEAP_MAX_PREMIUM", "0.40"))
CHEAP_MIN_SIZE      = int(os.getenv("CHEAP_MIN_SIZE", "50"))
CHEAP_MIN_NOTIONAL  = float(os.getenv("CHEAP_MIN_NOTIONAL", "5000"))

# Unusual sweeps
UNUSUAL_MIN_NOTIONAL  = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "75000"))
UNUSUAL_MIN_SIZE      = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))
UNUSUAL_MAX_DTE       = int(os.getenv("UNUSUAL_MAX_DTE", "45"))

# Whale flows
WHALES_MIN_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "300000"))
WHALES_MIN_SIZE     = int(os.getenv("WHALES_MIN_SIZE", "75"))
WHALES_MAX_DTE      = int(os.getenv("WHALES_MAX_DTE", "90"))

# Underlying price floor (ignore sub-penny / micro-cap garbage by default)
MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5.0"))

# Universe size
MAX_UNIVERSE = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", "120"))

# Per-day de-dupe (per category, per contract)
_alert_date: date | None = None
_seen_cheap: set[str] = set()
_seen_unusual: set[str] = set()
_seen_whale: set[str] = set()
_seen_ivcrush: set[str] = set()  # IV crush per contract

# IV cache (prev day IV per underlying+expiry)
_iv_cache: dict = {}


def _reset_if_new_day() -> None:
    global _alert_date, _seen_cheap, _seen_unusual, _seen_whale, _seen_ivcrush
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _seen_cheap = set()
        _seen_unusual = set()
        _seen_whale = set()
        _seen_ivcrush = set()
        print("[options_flow] New trading day – reset seen-contract sets and IV cache.")


def _load_iv_cache() -> dict:
    global _iv_cache
    path = os.getenv("OPTIONS_IV_CACHE_PATH", "/tmp/options_iv_cache.json")
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                _iv_cache = json.load(f)
                if not isinstance(_iv_cache, dict):
                    _iv_cache = {}
    except Exception as e:
        print(f"[options_flow] iv_cache load error: {e}")
        _iv_cache = {}
    return _iv_cache


def _save_iv_cache() -> None:
    path = os.getenv("OPTIONS_IV_CACHE_PATH", "/tmp/options_iv_cache.json")
    try:
        with open(path, "w") as f:
            json.dump(_iv_cache, f)
    except Exception as e:
        print(f"[options_flow] iv_cache save error: {e}")


def _safe_float(x, default: float | None = None) -> Optional[float]:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_int(x, default: int | None = None) -> Optional[int]:
    try:
        if x is None:
            return default
        return int(x)
    except (TypeError, ValueError):
        return default


def _parse_option_symbol(sym: str):
    """
    Polygon option symbol example: O:TSLA251121C00450000

    Underlying: TSLA
    Expiry: 2025-11-21
    Call/Put: C or P
    Strike: 450.00
    """
    from datetime import date as _date

    if not sym or not sym.startswith("O:"):
        return None, None, None, None

    try:
        base = sym[2:]

        # find first digit (start of YYMMDD)
        idx = 0
        while idx < len(base) and not base[idx].isdigit():
            idx += 1
        if idx >= len(base):
            return None, None, None, None

        under = base[:idx]
        exp_raw = base[idx:idx+6]   # YYMMDD
        cp_char = base[idx+6:idx+7]  # C or P
        rest = base[idx+7:]
        strike_raw = rest[7:]   # 000450000

        yy = int("20" + exp_raw[0:2])
        mm = int(exp_raw[2:4])
        dd = int(exp_raw[4:6])
        expiry = _date(yy, mm, dd)

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
        return now_est()
    except Exception:
        # fallback
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def _resolve_universe() -> list[str]:
    """
    Decide which underlyings to scan for options flow.

    Priority:
      1. OPTIONS_FLOW_TICKER_UNIVERSE (comma-separated)
      2. TICKER_UNIVERSE (your existing global universe)
      3. dynamic top-volume universe from shared.py
    """
    from bots.shared import get_dynamic_top_volume_universe

    # Allow a dedicated override
    env = os.getenv("OPTIONS_FLOW_TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]

    # else fall back to your normal dynamic + global TICKER_UNIVERSE
    env2 = os.getenv("TICKER_UNIVERSE")
    if env2:
        return [t.strip().upper() for t in env2.split(",") if t.strip()]

    return get_dynamic_top_volume_universe(max_tickers=MAX_UNIVERSE, volume_coverage=0.90)


# ---------------- IV CRUSH CHECK ----------------

def _maybe_iv_crush(
    sym: str,
    contract: str,
    under: str,
    expiry: date | None,
    dte: int | None,
    opt: dict,
    size: int,
    time_str: str,
) -> None:
    """
    Lightweight IV Crush detection per contract:
      • short-dated (<= IVCRUSH_MAX_DTE)
      • sufficient volume/size
      • big IV drop vs cached prior IV for same underlying+expiry
    Fires an additional "IV CRUSH" alert, separate from cheap/unusual/whale.
    """
    from bots.shared import (
        IVCRUSH_MIN_IV_DROP_PCT,
        IVCRUSH_MAX_DTE,
        IVCRUSH_MIN_VOL,
        chart_link,
    )

    if contract in _seen_ivcrush:
        return
    if dte is None or dte < 0 or dte > IVCRUSH_MAX_DTE:
        return
    if size < IVCRUSH_MIN_VOL:
        return

    iv = opt.get("implied_volatility") or (opt.get("day") or {}).get("implied_volatility")
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        return
    if iv <= 0:
        return

    today_str = str(date.today())
    key = f"{under}:{expiry}"
    prev = _iv_cache.get(key)
    if prev and prev.get("date") != today_str:
        prev_iv = float(prev.get("iv") or 0.0)
    else:
        prev_iv = None

    # If no previous IV, we can't compute a drop yet
    if prev_iv is None or prev_iv <= 0:
        _iv_cache[key] = {"iv": iv, "date": today_str}
        return

    # Compute IV drop
    if prev_iv <= 0:
        return
    iv_drop_pct = (prev_iv - iv) / prev_iv * 100.0
    if iv_drop_pct < IVCRUSH_MIN_IV_DROP_PCT:
        return

    # Update cache with current IV
    _iv_cache[key] = {"iv": iv, "date": today_str}

    # Underlying price (for alert)
    under_px = _underlying_price_from_opt(opt)
    under_line = f"💰 Underlying ${under_px:.2f}" if under_px is not None else "💰 Underlying price N/A"

    implied_move_pct = iv * math.sqrt(1.0 / 252.0) * 100.0

    extra_lines = [
        f"🧊 IV CRUSH — {sym}",
        f"🕒 {time_str}",
        under_line,
        "────────────",
        f"🎯 Contract: {contract}",
        f"📅 Exp: {expiry.strftime('%Y-%m-%d')} · DTE: {dte}",
        f"📊 IV now: {iv * 100:.1f}% (prev {prev_iv * 100:.1f}%, drop {iv_drop_pct:.1f}%)",
        f"📦 Volume / Size: {size:,}",
        f"📉 Implied 1-day move: ≈ {implied_move_pct:.1f}%",
        f"🔗 Chart: {chart_link(sym)}",
    ]

    extra_text = "\n".join(extra_lines)

    # We don't know last trade underlying RVOL here → pass 0.0
    last_price_for_bot = under_px if under_px is not None else 0.0
    send_alert("iv_crush", sym, last_price_for_bot, 0.0, extra=extra_text)
    _seen_ivcrush.add(contract)


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
           - Independently, also check for IV CRUSH conditions and fire a
             separate IV CRUSH alert per contract per day.
           - Fire a single alert per contract per category per day.
    """
    if not POLYGON_KEY:
        print("[options_flow] POLYGON_KEY missing; skipping.")
        return

    if not _in_rth_window():
        print("[options_flow] outside RTH; skipping.")
        return

    _reset_if_new_day()
    _load_iv_cache()  # initialize cache from disk if present

    BOT_NAME = "options_flow"
    start_ts = time.time()
    alerts_sent = 0
    matched_contracts: set[str] = set()

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
            # Polygon's option snapshot schema can vary slightly. Be defensive:
            details = opt.get("details") or {}
            contract = (
                details.get("ticker")
                or opt.get("ticker")
                or details.get("option_symbol")
                or details.get("symbol")
            )
            if not contract:
                # If we don't know the contract symbol, we can't look up trades.
                continue

            # Last trade for this contract (v3 last/trade/options)
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            t_res = trade.get("results")
            if isinstance(t_res, list) and t_res:
                last = t_res[0]
            elif isinstance(t_res, dict):
                last = t_res
            else:
                # Some Polygon responses put the trade fields at the top level
                last = trade

            if not isinstance(last, dict):
                continue

            price = _safe_float(last.get("p") or last.get("price"))
            size = _safe_int(last.get("s") or last.get("size"))
            if price is None or size is None:
                continue
            if price <= 0 or size <= 0:
                continue

            notional = price * size * 100.0

            # -------- Resolve underlying, expiry, contract type --------

            # First, parse from the raw option symbol (works for O:TSLA251121C00450000)
            under_guess, expiry_guess, cp_raw, _strike = _parse_option_symbol(contract)

            # Then, prefer explicit metadata from the snapshot where available
            exp_str = details.get("expiration_date")
            expiry = expiry_guess
            if exp_str:
                try:
                    expiry = date.fromisoformat(exp_str)
                except Exception:
                    # keep expiry_guess
                    pass

            ua = details.get("underlying_asset") or opt.get("underlying_asset") or {}
            under = (
                ua.get("ticker")
                or ua.get("symbol")
                or under_guess
                or sym
            )

            dte = _days_to_expiry(expiry)
            if dte is None or dte < 0:
                continue

            cp = _contract_type(opt, cp_raw)
            under_px = _underlying_price_from_opt(opt)

            # Ignore tiny / illiquid penny stuff by default
            if under_px is not None and under_px < MIN_UNDERLYING_PRICE:
                continue

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
                and notional >= UNUSUAL_MIN_NOTIONAL
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

            # Even if no category, we still may want to check IV CRUSH
            _maybe_iv_crush(sym, contract, under, expiry, dte, opt, size, time_str)

            if not category:
                continue

            # ---------------- ALERT FORMATTING ----------------

            notional_rounded = round(notional)
            dte_str = f"{dte} days" if dte is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P" if cp == "PUT" else "?"

            # Base contract line (pretty)
            exp_str = expiry.strftime('%b %d %Y') if expiry else 'N/A'
            contract_line = f"{under} {exp_str} {cp_letter}"

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

            body_lines = [
                header,
                f"🕒 {time_str}",
                under_line,
                "────────────",
                f"🎯 Contract: {contract_line}",
                f"📦 Size: {size:,} · Notional: ≈ ${notional_rounded:,.0f}",
                f"📅 DTE: {dte_str}",
                f"📈 Side: {cp or 'Option'}",
                f"🔗 Chart: {chart_link(sym)}",
                "",
                desc,
            ]

            extra = "\n".join(body_lines)

            # Send alert
            send_alert(category, sym, under_px or 0.0, 0.0, extra=extra)

            # Mark as seen
            if category == "whale":
                _seen_whale.add(contract)
            elif category == "unusual":
                _seen_unusual.add(contract)
            elif category == "cheap":
                _seen_cheap.add(contract)

            matched_contracts.add(contract)
            alerts_sent += 1

    # Persist IV cache at end of scan
    _save_iv_cache()

    run_seconds = time.time() - start_ts
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matched_contracts),
            alerts=alerts_sent,
            runtime=run_seconds,
        )
    except Exception as e:
        print(f"[options_flow] record_bot_stats error: {e}")

    print("[options_flow] scan complete.")