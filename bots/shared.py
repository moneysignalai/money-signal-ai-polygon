import os
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import pytz
import requests
from datetime import datetime, timedelta

# ---------------- CONFIG / ENV ----------------

POLYGON_KEY = os.getenv("POLYGON_KEY")

MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "1.1"))
MIN_VOLUME_GLOBAL = int(os.getenv("MIN_VOLUME_GLOBAL", "150000"))
MIN_PREMARKET_DOLLAR_VOL = int(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "150000"))
MIN_PREMARKET_MOVE_PCT = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "1.0"))
MIN_PREMARKET_PRICE = float(os.getenv("MIN_PREMARKET_PRICE", "2.0"))

DYNAMIC_MAX_TICKERS = int(os.getenv("DYNAMIC_MAX_TICKERS", "1000"))
DYNAMIC_MAX_LOOKBACK_DAYS = int(os.getenv("DYNAMIC_MAX_LOOKBACK_DAYS", "5"))
DYNAMIC_VOLUME_COVERAGE = float(os.getenv("DYNAMIC_VOLUME_COVERAGE", "0.75"))

EARNINGS_MAX_FORWARD_DAYS = int(os.getenv("EARNINGS_MAX_FORWARD_DAYS", "7"))

DEBUG_FLOW_REASONS = os.getenv("DEBUG_FLOW_REASONS", "false").lower() == "true"

STATUS_STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")

TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

# ---------------- TIME HELPERS ----------------


def now_est() -> datetime:
    return datetime.now(pytz.timezone("America/New_York"))


def est_today_date() -> datetime.date:
    return now_est().date()


def _parse_polygon_ts_ms(ts: Optional[int]) -> Optional[datetime]:
    """
    Polygon / Massive timestamps are usually in milliseconds since epoch.
    """
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000.0, tz=pytz.utc).astimezone(
            pytz.timezone("America/New_York")
        )
    except Exception:
        return None


def _is_rth_est(dt: datetime) -> bool:
    """
    True if the timestamp is within regular trading hours 9:30–16:00 Eastern.
    """
    if dt is None:
        return False
    hour = dt.hour
    minute = dt.minute
    # Rough bounds: 9:30 <= t < 16:00
    return (hour > 9 or (hour == 9 and minute >= 30)) and (hour < 16)


def in_rth_window_est() -> bool:
    """
    Helper to quickly check current EST time vs. RTH.
    """
    return _is_rth_est(now_est())


# ---------------- TELEGRAM HELPERS ----------------


def _send_telegram_message(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[telegram] error sending message: {e}")


def send_alert(text: str) -> None:
    """
    Primary alerts channel (signals).
    """
    if TELEGRAM_TOKEN_ALERTS and TELEGRAM_CHAT_ALL:
        _send_telegram_message(TELEGRAM_TOKEN_ALERTS, TELEGRAM_CHAT_ALL, text)


def send_status(text: str) -> None:
    """
    Status / heartbeat channel; if a dedicated status token isn't set,
    we fall back to the alerts bot.
    """
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    if token and TELEGRAM_CHAT_ALL:
        _send_telegram_message(token, TELEGRAM_CHAT_ALL, text)


def report_status_error(component: str, message: str) -> None:
    """
    Used by bots + shared helpers to surface errors into the status channel.
    """
    prefix = f"⚠️ {component} — "
    send_status(prefix + message)


# ---------------- SIMPLE DISK STATS CACHE (for status_report.py) ----------------


@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    run_seconds: float = 0.0
    runs: int = 0
    last_run_est: Optional[str] = None  # ISO string in EST


def _load_stats() -> Dict[str, Any]:
    try:
        with open(STATUS_STATS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_stats(data: Dict[str, Any]) -> None:
    try:
        tmp_path = STATUS_STATS_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, STATUS_STATS_PATH)
    except Exception as e:
        print(f"[shared] error saving stats to {STATUS_STATS_PATH}: {e}")


def record_bot_stats(
    bot_name: str,
    scanned: int,
    matched: int,
    alerts: int,
    run_seconds: float,
) -> None:
    """
    Called by each bot at the end of a successful run to append stats that
    status_report.py later aggregates into the heartbeat.
    """
    stats = _load_stats()
    per_bot = stats.get("per_bot", {})
    entry = per_bot.get(bot_name, {})

    runs = entry.get("runs", 0) + 1
    hist = entry.get("history", [])
    hist.append(
        {
            "ts_est": now_est().isoformat(),
            "scanned": int(scanned),
            "matched": int(matched),
            "alerts": int(alerts),
            "run_seconds": float(run_seconds),
        }
    )
    # keep last ~100
    hist = hist[-100:]

    entry.update(
        {
            "bot_name": bot_name,
            "runs": runs,
            "history": hist,
            "last_run_est": now_est().isoformat(),
        }
    )
    per_bot[bot_name] = entry
    stats["per_bot"] = per_bot
    _save_stats(stats)


# ---------------- HTTP / POLYGON HELPERS ----------------


def _http_get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    if params is None:
        params = {}
    if POLYGON_KEY and "apiKey" not in params:
        params["apiKey"] = POLYGON_KEY

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        msg = f"[shared] HTTP error for {url}: {e}"
        print(msg)
        report_status_error("shared:http", msg)
        return {}


def _paginate_v3(
    base_url: str,
    params: Dict[str, Any],
    limit: int = 50000,
    max_pages: int = 100,
    timeout: float = 20.0,
) -> List[Dict[str, Any]]:
    """
    Generic helper for v3-style pagination (results / next_url).
    """
    out: List[Dict[str, Any]] = []
    url = base_url
    params = dict(params)

    for _ in range(max_pages):
        data = _http_get_json(url, params=params, timeout=timeout)
        results = data.get("results") or []
        if isinstance(results, list):
            out.extend(results)
        elif isinstance(results, dict):
            out.append(results)

        next_url = data.get("next_url") or data.get("nextUrl")
        if not next_url:
            break
        url = next_url
        params = {}  # already has apiKey embedded in next_url

    return out


# ---------------- DYNAMIC UNIVERSE (STOCKS) ----------------


def _list_aggs(
    symbol: str,
    timespan: str,
    from_: str,
    to_: str,
    multiplier: int = 1,
    limit: int = 50000,
) -> List[Dict[str, Any]]:
    """
    Thin wrapper around /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch aggregates.")
        return []

    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_}/{to_}"
    params = {"limit": limit, "apiKey": POLYGON_KEY}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") or []
        if isinstance(results, list):
            return results
        elif isinstance(results, dict):
            return [results]
        return []
    except Exception as e:
        msg = f"[shared] error fetching aggs for {symbol}: {e}"
        print(msg)
        report_status_error("shared:aggs", msg)
        return []


def _compute_rvol_and_dollar_volume(
    daily_bars: List[Dict[str, Any]],
) -> Tuple[Optional[float], Optional[float]]:
    """
    Compute RVOL and today's dollar volume from daily bars.
    Expects each bar to have c (close), v (volume), and t (millis timestamp).
    """
    if not daily_bars or len(daily_bars) < 2:
        return None, None

    # sort ascending by time
    bars = sorted(daily_bars, key=lambda x: x.get("t", 0))
    # last is "today", preceding are history
    today = bars[-1]
    past = bars[:-1]

    v_today = today.get("v")
    c_today = today.get("c")
    if not v_today or not c_today:
        return None, None

    vols = [b.get("v") for b in past if b.get("v")]
    if len(vols) < 3:
        return None, None

    avg_vol = sum(vols) / len(vols)
    if avg_vol <= 0:
        return None, None

    rvol = v_today / avg_vol
    dollar_vol = v_today * c_today
    return float(rvol), float(dollar_vol)


def debug_filter_reason(bot: str, symbol: str, reason: str) -> None:
    if not DEBUG_FLOW_REASONS:
        return
    print(f"[{bot}] filtered {symbol}: {reason}")


def resolve_universe_for_bot(
    bot_name: str,
    base_universe: Optional[List[str]] = None,
    max_tickers: Optional[int] = None,
) -> List[str]:
    """
    Generic dynamic universe helper used by several bots:

      1) If a specific TICKER_UNIVERSE-like env is set for this bot, use that.
      2) Else, if a global TICKER_UNIVERSE is set, use that.
      3) Else, derive a dynamic universe based on recent dollar volume.

    The overall size is constrained by max_tickers if provided, otherwise
    DYNAMIC_MAX_TICKERS.
    """
    # Bot-specific override
    env_name = f"{bot_name.upper()}_TICKER_UNIVERSE"
    override = os.getenv(env_name)
    if override:
        syms = [s.strip().upper() for s in override.split(",") if s.strip()]
        return syms[: max_tickers or DYNAMIC_MAX_TICKERS]

    # Global override
    global_universe = os.getenv("TICKER_UNIVERSE") or os.getenv("FALLBACK_TICKER_UNIVERSE")
    if global_universe:
        syms = [s.strip().upper() for s in global_universe.split(",") if s.strip()]
        return syms[: max_tickers or DYNAMIC_MAX_TICKERS]

    # If caller passed a base_universe, use that
    if base_universe:
        syms = [s.strip().upper() for s in base_universe if s.strip()]
        return syms[: max_tickers or DYNAMIC_MAX_TICKERS]

    # Dynamic universe (fallback): pick most liquid tickers by recent dollar volume
    if not POLYGON_KEY:
        print("[shared] no POLYGON_KEY; cannot build dynamic universe.")
        return []

    today = est_today_date()
    from_date = today - timedelta(days=DYNAMIC_MAX_LOOKBACK_DAYS)
    from_str = from_date.strftime("%Y-%m-%d")
    to_str = today.strftime("%Y-%m-%d")

    # Use a broad index ETF universe by default
    base = os.getenv("FALLBACK_TICKER_UNIVERSE", "SPY,QQQ,VOO,IWM").split(",")
    base = [s.strip().upper() for s in base if s.strip()]

    candidates: Dict[str, float] = {}
    for sym in base:
        daily = _list_aggs(sym, "day", from_str, to_str, multiplier=1)
        rvol, dollar_vol = _compute_rvol_and_dollar_volume(daily)
        if dollar_vol is None:
            continue
        candidates[sym] = dollar_vol

    # Sort by dollar volume desc and take up to coverage * max_tickers
    max_n = max_tickers or DYNAMIC_MAX_TICKERS
    target_n = int(max_n * DYNAMIC_VOLUME_COVERAGE)
    sorted_syms = [s for s, _ in sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)]
    return sorted_syms[:target_n]


# ---------------- OPTIONS SNAPSHOT / CHAIN ----------------


@dataclass
class OptionCacheEntry:
    ts: float
    data: Dict[str, Any]


_OPTION_CACHE: Dict[str, OptionCacheEntry] = {}


def _cache_key(prefix: str, *parts: str) -> str:
    return prefix + ":" + ":".join(parts)


def get_option_chain_cached(
    underlying: str,
    ttl_seconds: int = 60,
) -> Optional[Dict[str, Any]]:
    """
    Fetches the option snapshot chain for a given underlying using the
    v3 /v3/snapshot/options/{underlying} endpoint, with a short TTL cache.
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
    try:
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 404:
            # Underlying might not have options
            return None
        resp.raise_for_status()
        data = resp.json()
        _OPTION_CACHE[key] = OptionCacheEntry(ts=now_ts, data=data)
        return data
    except Exception as e:
        msg = f"[shared] error fetching option chain for {underlying}: {e}"
        print(msg)
        report_status_error("shared:option_chain", msg)
        return None


def get_last_option_trades_cached(
    full_option_symbol: str,
    ttl_seconds: int = 45,
) -> Optional[Dict[str, Any]]:
    """
    Fetches the last option trade for a specific contract using the
    current Polygon/Massive v2 last-trade endpoint (/v2/last/trade/{optionsTicker}).

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

    # ✅ Correct Polygon/Massive options last-trade endpoint: /v2/last/trade/{optionsTicker}
    url = f"https://api.polygon.io/v2/last/trade/{full_option_symbol}"
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


def chart_link(symbol: str, timeframe: str = "5") -> str:
    return f"https://www.tradingview.com/chart/?symbol={symbol.upper()}"