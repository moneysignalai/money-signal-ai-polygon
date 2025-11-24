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


# ---------------- TELEGRAM CORE ----------------


def _send_telegram_raw(token: str, chat_id: str, text: str, parse_mode: Optional[str] = None) -> None:
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


# ---------------- DYNAMIC UNIVERSE ----------------


def get_dynamic_top_volume_universe(
    max_tickers: int = 100,
    volume_coverage: float = 0.90,
) -> List[str]:
    """
    Use Polygon's previous day's most-active (by dollar volume) to build a liquid universe.

    Used by multiple scanners (equity_flow, intraday_flow, trend_flow, options_flow, etc.).

    Mode A (stable): we also allow env overrides to keep the universe size sane:
      • DYNAMIC_MAX_TICKERS       — global cap on tickers regardless of caller's request
      • DYNAMIC_VOLUME_COVERAGE   — override coverage fraction (e.g. 0.95)
      • DYNAMIC_MAX_LOOKBACK_DAYS — how many days back we walk if a day is "dead"
      • FALLBACK_TICKER_UNIVERSE  — comma list of tickers if Polygon returns nothing usable
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot build dynamic universe.")
        return []

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

    max_lookback_days = 0
    try:
        max_lookback_days = int(os.getenv("DYNAMIC_MAX_LOOKBACK_DAYS", "5"))
    except Exception:
        max_lookback_days = 5
    max_lookback_days = max(1, min(max_lookback_days, 10))

    today = today_est_date()

    enriched: List[Tuple[str, float, float]] = []
    used_from_date: Optional[str] = None

    # Walk back up to N days until we find a day with real dollar volume
    for days_back in range(1, max_lookback_days + 1):
        prev = today - timedelta(days=days_back)
        from_ = prev.isoformat()

        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{from_}"
        params = {"adjusted": "true", "apiKey": POLYGON_KEY}

        data = _http_get_json(url, params, tag="shared:universe", timeout=20.0, retries=2)
        if not data:
            # error already reported inside _http_get_json; try the previous day
            continue

        results = data.get("results") or []
        tmp_enriched: List[Tuple[str, float, float]] = []
        for row in results:
            sym = row.get("T")
            vol = float(row.get("v") or 0.0)
            vwap = float(row.get("vw") or 0.0)
            dollar_vol = vol * max(vwap, 0.0)
            if not sym or dollar_vol <= 0:
                continue
            tmp_enriched.append((sym, vol, dollar_vol))

        if tmp_enriched:
            enriched = tmp_enriched
            used_from_date = from_
            break
        else:
            print(f"[shared] dynamic universe: 0 names for {from_}; trying prior day...")

    # If we still have nothing after walking back, use a fallback universe
    if not enriched:
        fallback_env = os.getenv("FALLBACK_TICKER_UNIVERSE")
        if fallback_env:
            universe = [t.strip().upper() for t in fallback_env.split(",") if t.strip()]
            if universe:
                universe = universe[:max_tickers]
                print(
                    f"[shared] dynamic universe empty across lookback; "
                    f"using FALLBACK_TICKER_UNIVERSE with {len(universe)} names."
                )
                return universe

        # Hard-coded ultra-liquid fallback if everything else fails
        fallback = ["SPY", "QQQ", "IWM", "TSLA", "NVDA", "AAPL", "MSFT", "META", "AMZN"]
        universe = fallback[:max_tickers]
        print(
            "[shared] dynamic universe empty across lookback; "
            "using static fallback universe (top ETFs/megacaps)."
        )
        return universe

    # Normal path: sort by dollar volume and take top names until coverage/max_tickers
    enriched.sort(key=lambda x: x[2], reverse=True)

    universe: List[str] = []
    total_dollar = sum(row[2] for row in enriched)
    running = 0.0
    for sym, _v, dv in enriched:
        universe.append(sym)
        running += dv
        if len(universe) >= max_tickers:
            break
        if total_dollar > 0 and running / total_dollar >= volume_coverage:
            break

    date_str = used_from_date or "unknown"
    print(
        f"[shared] dynamic universe (from {date_str}): "
        f"{len(universe)} names, covers ~{volume_coverage*100:.0f}% vol."
    )
    return universe


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

    url = f"https://api.polygon.io/v3/last/trade/{full_option_symbol}"
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