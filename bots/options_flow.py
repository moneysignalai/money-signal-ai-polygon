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
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Set

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
    is_bot_test_mode,
    is_bot_disabled,
    debug_filter_reason,
    resolve_universe_for_bot,  # ✅ central universe resolver
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

# IV crush settings
IVCRUSH_MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "25"))
IVCRUSH_MIN_VOL         = int(os.getenv("IVCRUSH_MIN_VOL", "100"))
IVCRUSH_MAX_DTE         = int(os.getenv("IVCRUSH_MAX_DTE", "21"))

# Underlying price floor (ignore sub-penny / micro-cap garbage by default)
MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5.0"))

# Universe size
MAX_UNIVERSE = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", "120"))

# Per-day de-dupe (per category, per contract)
_alert_date: Optional[date] = None
_seen_cheap: Set[str] = set()
_seen_unusual: Set[str] = set()
_seen_whale: Set[str] = set()
_seen_ivcrush: Set[str] = set()  # IV crush per contract

# IV cache (prev day IV per underlying+expiry)
_iv_cache: Dict[str, Any] = {}


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


def _load_iv_cache() -> Dict[str, Any]:
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


def _safe_float(x, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_int(x, default: Optional[int] = None) -> Optional[int]:
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
        core = sym[2:]  # drop 'O:'
        under = core[:-15]
        date_part = core[-15:-9]  # YYMMDD
        cp = core[-9:-8]
        strike_part = core[-8:]

        yy = int(date_part[0:2])
        mm = int(date_part[2:4])
        dd = int(date_part[4:6])
        year = 2000 + yy
        expiry = _date(year, mm, dd)

        strike = float(strike_part) / 1000.0
        if cp == "C":
            cptype = "CALL"
        elif cp == "P":
            cptype = "PUT"
        else:
            cptype = None
        return under, expiry, cptype, strike
    except Exception:
        return None, None, None, None


def _days_to_expiry(expiry: Optional[date]) -> Optional[int]:
    if not expiry:
        return None
    today = date.today()
    delta = (expiry - today).days
    return delta


def _contract_type(opt: Dict[str, Any], cp_raw: Optional[str]) -> Optional[str]:
    cp = cp_raw
    if not cp:
        cp = opt.get("contract_type") or opt.get("type")
    if isinstance(cp, str):
        c = cp.upper()[0]
        if c == "C":
            return "CALL"
        if c == "P":
            return "PUT"
    return None


def _underlying_price_from_opt(opt: Dict[str, Any]) -> Optional[float]:
    ua = opt.get("underlying_asset") or {}
    last = ua.get("last_price") or ua.get("lastTrade") or {}
    if isinstance(last, dict):
        price = last.get("price") or last.get("p")
    else:
        price = last
    return _safe_float(price)


def _format_time() -> str:
    return now_est()


# ---------------- UNIVERSE RESOLUTION ----------------

def _resolve_universe() -> List[str]:
    """
    Decide which underlyings to scan for options flow.

    Priority (handled by shared.resolve_universe_for_bot):
      1. OPTIONS_FLOW_TICKER_UNIVERSE (comma-separated)
      2. TICKER_UNIVERSE (your existing global universe)
      3. dynamic top-volume universe / FALLBACK_TICKER_UNIVERSE
    """
    return resolve_universe_for_bot(
        bot_name="options_flow",
        max_tickers=MAX_UNIVERSE,
        bot_env_var="OPTIONS_FLOW_TICKER_UNIVERSE",
    )


# ---------------- IV CRUSH CHECK ----------------

def _maybe_iv_crush(
    sym: str,
    contract: str,
    under: str,
    expiry: Optional[date],
    dte: Optional[int],
    opt: Dict[str, Any],
    size: int,
    time_str: str,
) -> None:
    """
    Track implied volatility per underlying+expiry and fire an alert when IV
    drops sharply (IVCRUSH_MIN_IV_DROP_PCT) on decent volume (IVCRUSH_MIN_VOL).
    """
    from datetime import date as _date

    # Only care about reasonably short-dated options
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

    today_str = str(_date.today())
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
    drop_pct = (prev_iv - iv) / prev_iv * 100.0
    if drop_pct < IVCRUSH_MIN_IV_DROP_PCT:
        return

    # Avoid spamming multiple IV crush alerts for same contract/day
    if contract in _seen_ivcrush:
        return

    # Build alert body
    header = f"🧊 IV CRUSH — {sym}"
    exp_str = expiry.strftime("%b %d %Y") if expiry else "N/A"
    body_lines = [
        header,
        f"🕒 {time_str}",
        "────────────",
        f"🎯 Contract: {under} {exp_str}",
        f"📦 Size: {size:,}",
        f"📉 IV Drop: {drop_pct:.1f}%",
        f"🔗 Chart: {chart_link(sym)}",
        "",
        "Big IV crush on short-dated options.",
    ]
    extra_text = "\n".join(body_lines)

    # We don't need super precise underlying here → pass 0.0
    last_price_for_bot = 0.0
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

    BOT_NAME = "options_flow"
    if is_bot_disabled(BOT_NAME):
        print("[options_flow] disabled via DISABLED_BOTS; skipping.")
        return

    if not _in_rth_window():
        print("[options_flow] outside RTH; skipping.")
        return

    _reset_if_new_day()
    _load_iv_cache()  # initialize cache from disk if present

    test_mode = is_bot_test_mode(BOT_NAME)
    start_ts = time.time()
    alerts_sent = 0
    matched_contracts: Set[str] = set()

    universe = _resolve_universe()
    if not universe:
        print("[options_flow] empty universe; skipping.")
        return

    print(f"[options_flow] scanning {len(universe)} symbols")

    time_str = _format_time()

    for sym in universe:
        if is_etf_blacklisted(sym):
            debug_filter_reason("options_flow", sym, "ETF blacklisted")
            continue

        chain = get_option_chain_cached(sym)
        if not chain:
            debug_filter_reason("options_flow", sym, "no option chain from Polygon")
            continue

        opts = chain.get("results") or chain.get("result") or chain.get("options") or []
        if not isinstance(opts, list) or not opts:
            debug_filter_reason("options_flow", sym, "option chain results empty")
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
                debug_filter_reason("options_flow", sym, "no contract symbol in snapshot")
                continue

            # Last trade for this contract (v3 last/trade/options)
            trade = get_last_option_trades_cached(contract)
            if not trade:
                debug_filter_reason("options_flow", sym, f"no last trade for {contract}")
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
                debug_filter_reason("options_flow", sym, f"last-trade payload not dict for {contract}")
                continue

            price = _safe_float(last.get("p") or last.get("price"))
            size = _safe_int(last.get("s") or last.get("size"))
            if price is None or size is None:
                debug_filter_reason("options_flow", sym, f"missing price/size for {contract}")
                continue
            if price <= 0 or size <= 0:
                debug_filter_reason("options_flow", sym, f"non-positive price/size for {contract}")
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
                debug_filter_reason("options_flow", sym, f"invalid DTE for {contract}")
                continue

            cp = _contract_type(opt, cp_raw)
            under_px = _underlying_price_from_opt(opt)

            # Ignore tiny / illiquid penny stuff by default
            if under_px is not None and under_px < MIN_UNDERLYING_PRICE:
                debug_filter_reason(
                    "options_flow",
                    sym,
                    f"underlying below min price ({under_px:.2f} < {MIN_UNDERLYING_PRICE})",
                )
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

            # UNUSUAL
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
                notional_rounded = round(notional)
                debug_filter_reason(
                    "options_flow",
                    sym,
                    f"no category match for {contract} "
                    f"(dte={dte}, size={size}, notional≈${notional_rounded:,.0f}, price={price:.2f})",
                )
                continue

            # ---------------- ALERT FORMATTING ----------------

            notional_rounded = round(notional)
            dte_str = f"{dte} days" if dte is not None else "N/A"
            cp_letter = "C" if cp == "CALL" else "P" if cp == "PUT" else "?"

            # Base contract line (pretty but simple – no raw O: ticker)
            exp_str2 = expiry.strftime('%b %d %Y') if expiry else 'N/A'
            contract_line = f"{under} {exp_str2} {cp_letter}"

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

            # Send alert (respects test_mode)
            if test_mode:
                print(
                    f"[options_flow:test] would alert {category.upper()} "
                    f"{contract} on {sym} · size={size} notional≈${notional_rounded:,.0f}"
                )
            else:
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