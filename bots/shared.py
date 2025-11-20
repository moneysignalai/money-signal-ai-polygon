# bots/shared.py — MoneySignalAI core utilities
#
# - Telegram alert helpers
# - Polygon universe helper with 3-min cache
# - Option-chain + last-trade helpers with cache
# - Common grading / filters

import os
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
import pytz

# ---------------- TIME / ENV ----------------

eastern = pytz.timezone("US/Eastern")


def now_est() -> datetime:
    """Return current datetime in US/Eastern timezone."""
    return datetime.now(eastern)


# --- API keys / global thresholds ---

POLYGON_KEY: Optional[str] = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")

MIN_RVOL_GLOBAL: float = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL: float = float(os.getenv("MIN_VOLUME_GLOBAL", "500000"))  # shares

# Telegram routing
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")

TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS")  # kept for compatibility, not required

# ---------------- TELEGRAM HELPERS ----------------


def _send_telegram_raw(token: Optional[str], chat_id: Optional[str], text: str) -> None:
    """Low-level Telegram sender. Logs failures but never raises."""
    if not token or not chat_id:
        print(f"[telegram] missing token/chat_id, message not sent: {text[:160]!r}")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"[telegram] send failed {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[telegram] exception sending telegram: {e}")


def send_alert(bot: str, ticker: str, price: float, rvol: float, extra: str = "") -> None:
    """High-level alert sender used by all bots.

    Most bots pass a fully formatted `extra` message (with emojis, dividers, etc.).
    In that case we send it as-is. If `extra` is empty, we fall back to a simple
    generic header using bot / ticker / price / rvol.
    """
    if extra:
        msg = extra
    else:
        msg = (
            f"🔔 *{bot.upper()} — {ticker}*\n"
            f"💰 ${price:.2f} · 📊 RVOL {rvol:.1f}x"
        )

    _send_telegram_raw(TELEGRAM_TOKEN_ALERTS, TELEGRAM_CHAT_ALL, msg)


def send_status(message: str) -> None:
    """Status / heartbeat messages.

    Prefer the dedicated status bot token if configured; otherwise fall back
    to the main alerts token. Always send to TELEGRAM_CHAT_ALL so there is
    a single central status/alert feed.
    """
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[status] (no telegram config) {message}")
        return
    _send_telegram_raw(token, chat, message)


# ---------------- ETF BLACKLIST ----------------

# Basic ETF blacklist; can be extended via ENV if you want
_DEFAULT_ETF_BLACKLIST = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "XLF",
    "XLE",
    "XLK",
    "XLV",
}

_env_etf = {s.strip().upper() for s in os.getenv("ETF_BLACKLIST", "").split(",") if s.strip()}
ETF_BLACKLIST = _DEFAULT_ETF_BLACKLIST.union(_env_etf)


def is_etf_blacklisted(ticker: str) -> bool:
    return ticker.upper() in ETF_BLACKLIST


# ---------------- CHART LINKS ----------------


def chart_link(symbol: str) -> str:
    """
    Build a TradingView link for the given underlying symbol.

    Example:
      https://www.tradingview.com/chart/?symbol=AAPL
    """
    sym = symbol.upper()
    return f"https://www.tradingview.com/chart/?symbol={sym}"


# ---------------- UNIVERSE / MOST ACTIVE (3-MIN CACHE) ----------------

_UNIVERSE_CACHE: Dict[str, Any] = {
    "ts": 0.0,
    "tickers": ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA"],
}


def get_dynamic_top_volume_universe(
    max_tickers: int = 100,
    volume_coverage: float = 0.90,
) -> List[str]:
    """Approximate 'top N names that capture ~X% of market volume'.

    Implementation:
      • Uses Polygon's "previous close" / snapshot endpoints as a proxy for
        current volume leaders.
      • Keeps a 3-minute in-memory cache to avoid hammering the API.
      • Fallback: if Polygon fails or returns nothing, returns a static
        baseline universe from the last cache.
    """
    now = time.time()
    if now - _UNIVERSE_CACHE["ts"] < 180 and _UNIVERSE_CACHE["tickers"]:
        return _UNIVERSE_CACHE["tickers"]

    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; using cached/static universe.")
        return _UNIVERSE_CACHE["tickers"]

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"apiKey": POLYGON_KEY, "limit": 1000}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] error fetching dynamic universe: {e}")
        return _UNIVERSE_CACHE["tickers"]

    results = data.get("tickers") or []
    if not results:
        print("[shared] no tickers in dynamic universe response.")
        return _UNIVERSE_CACHE["tickers"]

    def _vol(rec: Dict[str, Any]) -> float:
        try:
            day = rec.get("day") or {}
            return float(day.get("v", 0.0))
        except Exception:
            return 0.0

    # Sort by day volume descending
    sorted_by_vol = sorted(results, key=_vol, reverse=True)

    # Compute cumulative coverage and pick max_tickers or volume_coverage threshold, whichever hits first
    total_vol = sum(_vol(r) for r in sorted_by_vol) or 1.0
    picked: List[str] = []
    running = 0.0

    for rec in sorted_by_vol:
        t = rec.get("ticker")
        if not t:
            continue
        v = _vol(rec)
        running += v
        picked.append(t)
        if running / total_vol >= volume_coverage or len(picked) >= max_tickers:
            break

    if not picked:
        picked = _UNIVERSE_CACHE["tickers"]

    _UNIVERSE_CACHE["ts"] = now
    _UNIVERSE_CACHE["tickers"] = picked
    return picked


# ---------------- OPTION CHAIN CACHE ----------------

_OPTION_CACHE: Dict[str, Dict[str, Any]] = {}


def get_option_chain_cached(ticker: str) -> Optional[Dict[str, Any]]:
    """Get snapshot option chain for an underlying with a short cache.

    Cache behaviour:
      • 30-second TTL per underlying.
      • Uses Polygon snapshot options endpoint.
    """
    key = f"chain_{ticker.upper()}"
    now = time.time()

    # Cache hit
    entry = _OPTION_CACHE.get(key)
    if entry and now - entry.get("ts", 0) < 30:
        return entry.get("data")

    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY not set; cannot fetch options.")
        return None

    url = f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] error fetching option chain for {ticker}: {e}")
        return None

    # Store in cache
    _OPTION_CACHE[key] = {"ts": now, "data": data}
    return data


def get_last_option_trades_cached(full_option_symbol: str) -> Optional[Dict[str, Any]]:
    """Get the last trade for a single option symbol with a short cache.

    Behaviour:
      • 30-second in-memory cache per option.
      • On HTTP 404 (no trade / not found), returns None and logs softly.
      • On other HTTP / network errors, logs and returns None.
    """
    key = f"trade_{full_option_symbol}"
    now = time.time()

    # Cache hit
    entry = _OPTION_CACHE.get(key)
    if entry and now - entry.get("ts", 0) < 30:
        return entry.get("data")

    if not POLYGON_KEY:
        return None

    url = f"https://api.polygon.io/v2/last/trade/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        # Treat 404 (no data) as a normal, non-fatal condition
        if r.status_code == 404:
            print(f"[shared] no last option trade for {full_option_symbol} (404).")
            return None

        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] error fetching last option trade for {full_option_symbol}: {e}")
        return None

    _OPTION_CACHE[key] = {"ts": now, "data": data}
    return data


def get_last_option_trade_cached(full_option_symbol: str) -> Optional[Dict[str, Any]]:
    """
    Backwards-compat alias for older bots that imported the singular name.
    Delegates to get_last_option_trades_cached().
    """
    return get_last_option_trades_cached(full_option_symbol)


def getLastOptionTradesCached(full_option_symbol: str) -> Optional[Dict[str, Any]]:
    """
    Backwards-compat alias for older camelCase import style.
    """
    return get_last_option_trades_cached(full_option_symbol)


def getlastoptiontradescached(full_option_symbol: str) -> Optional[Dict[str, Any]]:
    """
    Backwards-compat alias for fully lowercase import style (defensive).
    """
    return get_last_option_trades_cached(full_option_symbol)


# ---------------- GRADING / SETUP QUALITY ----------------


def grade_equity_setup(
    move_pct: float,
    rvol: float,
    dollar_vol: float,
) -> str:
    """Very rough A+/A/B/C grade for an equity setup.

    Inputs:
      • move_pct: absolute % move from a reference (e.g. ORB break, gap, etc.)
      • rvol: relative volume multiple vs typical
      • dollar_vol: traded notional in USD

    Heuristic:
      - movement:   0–2pt → low, 2–4 modest, 4–8 strong, >8 explosive
      - RVOL:       1–2 moderate, 2–4 high, >4 extreme
      - $ volume:   5–25M light, 25–75M solid, 75–200M large, >200M huge

    Returns:
      "A+", "A", "B", or "C"
    """
    score = 0

    # Move contribution
    m = abs(move_pct)
    if m >= 2:
        score += 1
    if m >= 4:
        score += 1
    if m >= 8:
        score += 1

    # RVOL contribution
    if rvol >= 2:
        score += 1
    if rvol >= 4:
        score += 1

    # Dollar-volume contribution
    if dollar_vol >= 25_000_000:
        score += 1
    if dollar_vol >= 75_000_000:
        score += 1
    if dollar_vol >= 200_000_000:
        score += 1

    if score >= 6:
        return "A+"
    if score >= 4:
        return "A"
    if score >= 2:
        return "B"
    return "C"