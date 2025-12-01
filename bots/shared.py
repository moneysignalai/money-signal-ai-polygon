# bots/shared.py
import os
import time
import math
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pytz
import requests

# ---------------- BASIC CONFIG ----------------

POLYGON_KEY = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")

# Global RVOL / volume floors that other bots can reference
MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "500000"))  # shares

# Telegram routing (your env)
# - TELEGRAM_CHAT_ALL      = single private chat ID for everything
# - TELEGRAM_TOKEN_ALERTS  = all-in-one alerts bot for all trade signals
# - TELEGRAM_TOKEN_STATUS  = dedicated status/heartbeat bot
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

# Some bots may want a separate status-only chat; if not set, fallback to CHAT_ALL
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS") or TELEGRAM_CHAT_ALL

# ----------------------------------------------------------------------
# Bot-level config flags (for system-level control & debugging)
# ----------------------------------------------------------------------

# DISABLED_BOTS: comma-separated list of bot names that should NOT run at all.
# Example: DISABLED_BOTS=daily_ideas,options_indicator
DISABLED_BOTS = {
    b.strip().lower()
    for b in os.getenv("DISABLED_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# TEST_MODE_BOTS: comma-separated list of bot names that should run in "shadow" / test mode.
# Example: TEST_MODE_BOTS=options_flow,rsi_signals
TEST_MODE_BOTS = {
    b.strip().lower()
    for b in os.getenv("TEST_MODE_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# DEBUG_FLOW_REASONS: if true, bots can log why candidates were rejected by filters.
# Example: DEBUG_FLOW_REASONS=true
DEBUG_FLOW_REASONS = os.getenv("DEBUG_FLOW_REASONS", "false").lower() == "true"

eastern = pytz.timezone("US/Eastern")


# ---------------- TIME HELPERS ----------------


def now_est() -> str:
    """
    Human-friendly time string in Eastern, e.g. '10:48 AM EST · Nov 20'.

    NOTE: this returns a STRING on purpose, so bots can just do:
        ts = now_est()
    and drop it straight into messages.
    """
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def today_est_date() -> date:
    return datetime.now(eastern).date()


def iso_today() -> str:
    return today_est_date().isoformat()


def minutes_since_midnight_est() -> int:
    now = datetime.now(eastern)
    return now.hour * 60 + now.minute


# ----------------------------------------------------------------------
# Pretty-format helpers (for contracts / debug)
# ----------------------------------------------------------------------


def pretty_contract(raw: str) -> str:
    """
    Convert Polygon-style contract symbols like:
        O:TSLA251121C00450000
    into human-readable:
        TSLA 11/21/25 450C

    If parsing fails, returns the original string.
    """
    try:
        if not raw or not raw.startswith("O:"):
            return raw

        core = raw[2:]  # TSLA251121C00450000
        if len(core) < 15:
            return raw

        underlying = core[:-15]
        date_part = core[-15:-9]  # YYMMDD
        cp = core[-9:-8]          # C / P
        strike_part = core[-8:]   # 00450000 -> 450.000

        yy = int(date_part[0:2]) + 2000
        mm = int(date_part[2:4])
        dd = int(date_part[4:6])

        strike_int = int(strike_part)
        strike = strike_int / 1000.0

        exp_fmt = f"{mm:02d}/{dd:02d}/{str(yy)[2:]}"  # 11/21/25
        cp_letter = cp.upper() if cp in ("C", "P") else "?"

        return f"{underlying} {exp_fmt} {strike:g}{cp_letter}"
    except Exception:
        return raw


# ----------------------------------------------------------------------
# Bot-mode helper functions
# ----------------------------------------------------------------------


def is_bot_disabled(bot_name: str) -> bool:
    """
    Return True if this bot is globally disabled via env (DISABLED_BOTS).

    This is primarily used by scheduler / bots to early-exit.
    """
    return bot_name.strip().lower() in DISABLED_BOTS


def is_bot_test_mode(bot_name: str) -> bool:
    """
    Return True if this bot is in TEST_MODE_BOTS.

    Bots can use this to:
      • avoid sending real alerts (shadow mode), or
      • route alerts to a separate Telegram chat, etc.
    """
    return bot_name.strip().lower() in TEST_MODE_BOTS


def debug_filter_reason(bot_name: str, symbol: str, reason: str) -> None:
    """
    Optional debugging helper.

    If DEBUG_FLOW_REASONS=true, bots can call this to log why a candidate
    was rejected by filters. This is useful when you see scanned>0 but alerts=0
    and want to understand which filter is doing the blocking.

    Output is cleaned up:
      • Polygon option tickers (O:TSLA251121C00450000) are converted to:
            TSLA 11/21/25 450C
      • One-line, emoji-tagged, timestamped.
    """
    if not DEBUG_FLOW_REASONS:
        return

    # Clean up any Polygon-style option symbols in the reason text
    parts = reason.split()
    cleaned_parts: List[str] = []
    for p in parts:
        if p.startswith("O:"):
            cleaned_parts.append(pretty_contract(p))
        else:
            cleaned_parts.append(p)
    cleaned_reason = " ".join(cleaned_parts)

    ts = now_est()
    # Example:
    # 🐞 DEBUG — options_flow | TSLA | 10:15 AM EST · Nov 30 → no last trade for TSLA 11/21/25 450C
    print(f"🐞 DEBUG — {bot_name} | {symbol} | {ts} → {cleaned_reason}")


# ---------------- TELEGRAM CORE ----------------


def _send_telegram_raw(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: Optional[str] = None,
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        # We deliberately do not raise; status bot might still be able to report.
        print(f"[telegram] failed to send: {e} | text={text!r}")


def send_alert(
    bot_name: str,
    symbol: str,
    last_price: float,
    rvol: float,
    extra: Optional[str] = None,
) -> None:
    """
    Core alert sender used by all bots.
    Sends a pretty Telegram message via TELEGRAM_TOKEN_ALERTS → TELEGRAM_CHAT_ALL.
    """
    token = TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[alert:{bot_name}] (no TELEGRAM_TOKEN_ALERTS or TELEGRAM_CHAT_ALL) {symbol} {extra}")
        return

    header = f"🧠 {bot_name.upper()} — {symbol}"
    body_lines = [header]
    if last_price:
        body_lines.append(f"💰 Last: ${last_price:.2f}")
    if rvol:
        body_lines.append(f"📊 RVOL: {rvol:.1f}x")
    if extra:
        body_lines.append("────────────")
        body_lines.append(extra)

    text = "\n".join(body_lines)
    _send_telegram_raw(token, chat, text, parse_mode=None)


# ---------------- STATUS / ERROR REPORTING ----------------


def _send_status(text: str) -> None:
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_STATUS or TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[status] (no TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL) {text}")
        return
    _send_telegram_raw(token, chat, text, parse_mode=None)


def report_status_error(bot: str, message: str) -> None:
    """
    Soft error reporting to the status bot.

    Called by shared helpers when a non-fatal error happens (e.g. Polygon fails),
    so it shows up in Telegram even if the main bot continues.
    """
    ts = now_est()
    text = f"⚠️ [{bot}] {ts}\n{message}"
    _send_status(text)


# ---------------- HTTP HELPER WITH RETRIES ----------------


def _http_get_json(
    url: str,
    params: Dict[str, Any],
    *,
    tag: str,
    timeout: float = 20.0,
    retries: int = 2,
    backoff_seconds: float = 2.5,
) -> Optional[Dict[str, Any]]:
    """
    Thin wrapper around requests.get with:
      • configurable timeout
      • a few retries with exponential backoff
      • optional status reporting on final failure

    This is used for Polygon grouped/agg/snapshot GETs so that transient
    slowness does not kill the bots or flood them with exceptions.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            # Graceful handling of rate limits
            if resp.status_code == 429:
                wait = backoff_seconds * (attempt + 1)
                print(f"[{tag}] Polygon 429 rate-limit; sleeping {wait:.1f}s before retry.")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < retries:
                wait = backoff_seconds * (attempt + 1)
                print(f"[{tag}] HTTP error on attempt {attempt+1}/{retries+1}: {e} — retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                msg = f"[{tag}] error after {retries+1} attempts: {e}"
                print(msg)
                report_status_error(tag, msg)
                return None
    return None


# ---------------- TICKER UNIVERSE HELPERS ----------------


def _parse_ticker_env(raw: str, max_tickers: int) -> List[str]:
    """
    Parse a comma-separated env string into a cleaned ticker list.

    - Strips spaces and quotes
    - Uppercases tickers
    - Deduplicates while preserving order
    """
    if not raw:
        return []
    seen = set()
    out: List[str] = []
    for part in raw.replace('"', "").split(","):
        sym = part.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
        if len(out) >= max_tickers:
            break
    return out


def _fallback_universe_from_env(env_key: str, max_tickers: int, reason: str) -> List[str]:
    """
    Shared helper for pulling a ticker list from an env var (e.g. FALLBACK_TICKER_UNIVERSE).
    """
    raw = os.getenv(env_key, "")
    tickers = _parse_ticker_env(raw, max_tickers)
    if not tickers:
        print(f"[shared] {env_key} empty; returning []. (reason={reason})")
        return []
    print(f"[shared] using {env_key}: {len(tickers)} names (reason={reason})")
    return tickers


# ---------------- DYNAMIC / CONFIGURED UNIVERSE ----------------

_UNIVERSE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": []}


def get_dynamic_top_volume_universe(
    max_tickers: int = 100,
    volume_coverage: float = 0.90,
) -> List[str]:
    """
    Primary universe builder used by bots.

    PRECEDENCE:
      1) If TICKER_UNIVERSE env is set → ALWAYS use that (no Polygon call).
      2) Else, use Polygon previous-day grouped data to build a top-volume universe.
         • 60s in-process cache to avoid hammering Polygon
         • Faster timeouts (7s) + 1 retry
      3) If Polygon is unavailable/empty → FALLBACK_TICKER_UNIVERSE env (if set).

    This means:
      • Your system will only ever scan your hand-picked TICKER_UNIVERSE
        when you set it in Render env.
      • Dynamic Polygon universe is only used when TICKER_UNIVERSE is blank.
    """
    now_ts = time.time()

    # 1) Global override: TICKER_UNIVERSE
    ticker_universe_env = os.getenv("TICKER_UNIVERSE", "")
    tickers = _parse_ticker_env(ticker_universe_env, max_tickers)
    if tickers:
        print(f"[shared] using TICKER_UNIVERSE override: {len(tickers)} names")
        return tickers

    # 2) If no Polygon key, go straight to fallback
    if not POLYGON_KEY:
        return _fallback_universe_from_env(
            "FALLBACK_TICKER_UNIVERSE",
            max_tickers,
            reason="no POLYGON_KEY",
        )

    # 60-second cache to avoid repeated heavy calls
    if _UNIVERSE_CACHE["data"] and now_ts - float(_UNIVERSE_CACHE["ts"]) < 60.0:
        data: List[str] = _UNIVERSE_CACHE["data"]
        return data[:max_tickers]

    # Apply global caps from env for safety
    try:
        env_cap = int(os.getenv("DYNAMIC_MAX_TICKERS", str(max_tickers)))
        max_tickers = max(1, min(max_tickers, env_cap))
    except Exception:
        pass

    try:
        env_cov = os.getenv("DYNAMIC_VOLUME_COVERAGE")
        if env_cov is not None:
            volume_coverage = float(env_cov)
    except Exception:
        pass

    today = today_est_date()
    prev = today - timedelta(days=1)
    from_ = prev.isoformat()

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{from_}"
    params = {"adjusted": "true", "apiKey": POLYGON_KEY}

    data = _http_get_json(
        url,
        params,
        tag="shared:universe",
        timeout=7.0,
        retries=1,
        backoff_seconds=2.0,
    )

    results = data.get("results") if data else None
    if not results:
        # 3) Fallback when Polygon is slow/empty
        return _fallback_universe_from_env(
            "FALLBACK_TICKER_UNIVERSE",
            max_tickers,
            reason="Polygon empty/slow",
        )

    enriched: List[Tuple[str, float]] = []
    for row in results:
        sym = row.get("T")
        vol = float(row.get("v") or 0.0)
        vwap = float(row.get("vw") or 0.0)
        dollar_vol = vol * max(vwap, 0.0)
        if not sym or dollar_vol <= 0:
            continue
        enriched.append((sym, dollar_vol))

    if not enriched:
        return _fallback_universe_from_env(
            "FALLBACK_TICKER_UNIVERSE",
            max_tickers,
            reason="0 names after filtering",
        )

    enriched.sort(key=lambda x: x[1], reverse=True)

    universe: List[str] = []
    total_dollar = sum(row[1] for row in enriched)
    running = 0.0
    for sym, dv in enriched:
        universe.append(sym)
        running += dv
        if len(universe) >= max_tickers:
            break
        if total_dollar > 0 and running / total_dollar >= volume_coverage:
            break

    print(f"[shared] dynamic universe: {len(universe)} names, covers ~{volume_coverage*100:.0f}% vol.")
    _UNIVERSE_CACHE["ts"] = now_ts
    _UNIVERSE_CACHE["data"] = universe
    return universe


def resolve_universe(
    per_bot_env: Optional[str],
    max_tickers: int,
    volume_coverage: float = 0.90,
) -> List[str]:
    """
    Generic helper for bots to resolve their scanning universe.

    Usage examples:
      • Global-style bot:
            universe = resolve_universe(None, max_tickers=120)
      • Options flow bot with override:
            universe = resolve_universe("OPTIONS_FLOW_TICKER_UNIVERSE", max_tickers=OPTIONS_FLOW_MAX_UNIVERSE)

    Resolution order:
      1) If per_bot_env is provided AND non-empty → use that env list.
      2) Else → use get_dynamic_top_volume_universe(max_tickers, volume_coverage),
         which itself respects TICKER_UNIVERSE if set.
    """
    # 1) Per-bot override env, e.g. OPTIONS_FLOW_TICKER_UNIVERSE
    if per_bot_env:
        raw = os.getenv(per_bot_env, "")
        tickers = _parse_ticker_env(raw, max_tickers)
        if tickers:
            print(f"[shared] using {per_bot_env} override: {len(tickers)} names")
            return tickers

    # 2) Fall back to global universe logic (TICKER_UNIVERSE or Polygon or FALLBACK_TICKER_UNIVERSE)
    return get_dynamic_top_volume_universe(max_tickers=max_tickers, volume_coverage=volume_coverage)


# ---------------- ETF BLACKLIST ----------------

ETF_BLACKLIST = {
    "DIA",
    "VTI",
    "XLK",
    "XLF",
    "XLE",
    "XLY",
    "XLI",
    "XLV",
    "XLP",
    "XLB",
    "XLU",
    "XOP",
    "XRT",
}


def is_etf_blacklisted(symbol: str) -> bool:
    return symbol.upper() in ETF_BLACKLIST


# ---------------- CACHED UNDERLYING LAST ----------------

@dataclass
class LastTradeCacheEntry:
    ts: float
    last: float
    dollar_vol: float


_LAST_TRADE_CACHE: Dict[str, LastTradeCacheEntry] = {}


def get_last_trade_cached(symbol: str, ttl_seconds: int = 15) -> Tuple[Optional[float], Optional[float]]:
    """
    Cached last / dollar volume for the underlying (equity/ETF).
    Uses v2 last trade + MIN_VOLUME_GLOBAL as a rough dollar-vol approximation.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last trade.")
        return None, None

    key = symbol.upper()
    now_ts = time.time()
    entry = _LAST_TRADE_CACHE.get(key)
    if entry and isinstance(entry.ts, (int, float)) and now_ts - float(entry.ts) < ttl_seconds:
        return entry.last, entry.dollar_vol

    url = f"https://api.polygon.io/v2/last/trade/{symbol.upper()}"
    params = {"apiKey": POLYGON_KEY}

    data = _http_get_json(url, params, tag="shared:last_trade", timeout=15.0, retries=1)
    if not data:
        return None, None

    results = data.get("results")
    if isinstance(results, dict):
        last_raw = results.get("p")
    else:
        last_raw = None

    try:
        last_price = float(last_raw) if last_raw is not None else None
    except (TypeError, ValueError):
        last_price = None

    if last_price is None or last_price <= 0:
        return None, None

    dollar_vol = last_price * MIN_VOLUME_GLOBAL
    _LAST_TRADE_CACHE[key] = LastTradeCacheEntry(ts=now_ts, last=last_price, dollar_vol=dollar_vol)
    return last_price, dollar_vol


# ---------------- OPTION CACHES ----------------

@dataclass
class OptionCacheEntry:
    ts: float
    data: Dict[str, Any]


_OPTION_CACHE: Dict[str, OptionCacheEntry] = {}


def _cache_key(prefix: str, identifier: str) -> str:
    return f"{prefix}:{identifier}"


def get_option_chain_cached(
    underlying: str,
    ttl_seconds: int = 90,
) -> Optional[Dict[str, Any]]:
    """Fetches Polygon snapshot option chain via HTTP and caches it.

    Used by options_flow and any options-related logic.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch option chain.")
        return None

    key = _cache_key("chain", underlying.upper())
    now_ts = time.time()

    entry = _OPTION_CACHE.get(key)
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now_ts - float(entry.ts) < ttl_seconds:
            return entry.data

    url = f"https://api.polygon.io/v3/snapshot/options/{underlying.upper()}"
    params = {"apiKey": POLYGON_KEY}

    data = _http_get_json(url, params, tag="shared:option_chain", timeout=20.0, retries=1)
    if not data:
        return None

    _OPTION_CACHE[key] = OptionCacheEntry(ts=now_ts, data=data)
    return data


def get_last_option_trades_cached(
    full_option_symbol: str,
    ttl_seconds: int = 45,
) -> Optional[Dict[str, Any]]:
    """
    Fetches the last option trade for a specific contract (v3 last/trade).

    Improvements:
      • Slightly longer timeout (default ~20s).
      • One retry with backoff.
      • 404 is treated as benign (no status spam).
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last option trades.")
        return None

    key = _cache_key("last_trade", full_option_symbol)
    now_ts = time.time()

    entry = _OPTION_CACHE.get(key)
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now_ts - float(entry.ts) < ttl_seconds:
            return entry.data

    # ✅ Correct Polygon options last-trade endpoint
    url = f"https://api.polygon.io/v3/last/trade/options/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    timeout = 20.0
    retries = 1
    backoff_seconds = 2.5

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 404:
                # Benign: no last option trade exists yet for this contract.
                return None
            resp.raise_for_status()
            data = resp.json()
            _OPTION_CACHE[key] = OptionCacheEntry(ts=now_ts, data=data)
            return data
        except Exception as e:
            if attempt < retries:
                wait = backoff_seconds * (attempt + 1)
                print(
                    f"[shared:last_option_trade] HTTP error on attempt "
                    f"{attempt+1}/{retries+1}: {e} — retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                msg = f"[shared] error fetching last option trade for {full_option_symbol}: {e}"
                print(msg)
                report_status_error("shared:last_option_trade", msg)
                return None

    return None


# Legacy camelCase aliases
def getOptionChainCached(underlying: str, ttl_seconds: int = 60):
    return get_option_chain_cached(underlying, ttl_seconds=ttl_seconds)


def getLastOptionTradesCached(full_option_symbol: str, ttl_seconds: int = 30):
    return get_last_option_trades_cached(full_option_symbol, ttl_seconds=ttl_seconds)


# ---------------- CHART LINK ----------------


def chart_link(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={symbol.upper()}"


# ---------------- GRADING ----------------


def grade_equity_setup(
    move_pct: float,
    rvol: float,
    dollar_vol: float,
) -> str:
    """Simple letter grade: A+ / A / B / C based on strength."""
    score = 0.0

    score += max(0.0, min(rvol / 2.0, 3.0))  # up to 3 points
    score += max(0.0, min(abs(move_pct) / 3.0, 3.0))  # up to 3
    score += max(0.0, min(math.log10(max(dollar_vol, 1.0)) - 6.0, 2.0))  # up to 2

    if score >= 7.0:
        return "A+"
    if score >= 5.5:
        return "A"
    if score >= 4.0:
        return "B"
    return "C"


# ---------------- TIME WINDOWS HELPERS ----------------


def is_between_times(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    tz: pytz.timezone,
) -> bool:
    now = datetime.now(tz)
    mins = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= mins <= end


def is_rth() -> bool:
    """Regular trading hours 09:30–16:00 ET."""
    return is_between_times(9, 30, 16, 0, eastern)


def is_premarket() -> bool:
    return is_between_times(4, 0, 9, 29, eastern)


def is_postmarket() -> bool:
    return is_between_times(16, 1, 20, 0, eastern)