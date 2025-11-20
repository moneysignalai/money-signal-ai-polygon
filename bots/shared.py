"""
bots/shared.py — MoneySignalAI core utilities

Shared helpers used by all bots:

- Time / timezone helpers
- Telegram alert helpers
- Polygon universe helper with short cache
- Option-chain + last-trade helpers with cache
- Common grading / filters
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
import pytz

# ---------------- TIME / ENV ----------------

eastern = pytz.timezone("US/Eastern")


def now_est() -> datetime:
    """Return current time in US/Eastern."""
    return datetime.now(eastern)


def _fmt_est(ts: Optional[datetime] = None) -> str:
    if ts is None:
        ts = now_est()
    # If your Python version chokes on %-I, change to %I (it just adds a leading 0)
    return ts.strftime("%-I:%M %p EST · %b %d")


# --- API keys / global thresholds ---

POLYGON_KEY: Optional[str] = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")

# Global sanity thresholds some bots can reference
MIN_RVOL_GLOBAL: float = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL: float = float(os.getenv("MIN_VOLUME_GLOBAL", "500000"))

TELEGRAM_TOKEN_ALERTS: Optional[str] = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_CHAT_ALL: Optional[str] = os.getenv("TELEGRAM_CHAT_ALL")

TELEGRAM_TOKEN_STATUS: Optional[str] = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_CHAT_STATUS: Optional[str] = os.getenv("TELEGRAM_CHAT_STATUS")


# ---------------- TELEGRAM HELPERS ----------------


def _send_telegram_raw(token: Optional[str], chat_id: Optional[str], text: str) -> None:
    """Low-level Telegram sender with basic error handling."""
    if not token or not chat_id:
        print("[telegram] missing token/chat_id, skipping message:", text[:120])
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
    """
    High-level alert sender used by all bots.

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


def send_status(text: str) -> None:
    """Send a plain text status message to the status channel (if configured)."""
    _send_telegram_raw(TELEGRAM_TOKEN_STATUS, TELEGRAM_CHAT_STATUS, text)


# ---------------- ETF BLACKLIST ----------------

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

_env_etf = {
    s.strip().upper()
    for s in os.getenv("ETF_BLACKLIST", "").split(",")
    if s.strip()
}

ETF_BLACKLIST = _DEFAULT_ETF_BLACKLIST.union(_env_etf)


def is_etf_blacklisted(ticker: str) -> bool:
    return ticker.upper() in ETF_BLACKLIST


# ---------------- VIEW / CHART LINKS ----------------


def chart_link(symbol: str) -> str:
    """TradingView chart link helper used everywhere."""
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
    """
    Approximate "top N names that capture ~X% of market volume".

    - Uses Polygon v2 snapshot tickers endpoint.
    - Caches the full list for 3 minutes to reduce API load.
    - Falls back to the last good cache if the request fails.
    """
    global _UNIVERSE_CACHE

    # Quick cache hit path
    now = time.time()
    if now - _UNIVERSE_CACHE["ts"] < 180 and _UNIVERSE_CACHE["tickers"]:
        return _UNIVERSE_CACHE["tickers"][:max_tickers]

    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY not set; using cached fallback universe.")
        return _UNIVERSE_CACHE["tickers"][:max_tickers]

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"apiKey": POLYGON_KEY, "limit": 1000}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] snapshot tickers fetch failed: {e}")
        return _UNIVERSE_CACHE["tickers"][:max_tickers]

    results = data.get("tickers") or []
    if not results:
        print("[shared] snapshot tickers returned empty result set.")
        return _UNIVERSE_CACHE["tickers"][:max_tickers]

    # Sort by daily volume desc
    def _vol(rec: Dict[str, Any]) -> float:
        try:
            return float(rec.get("v", 0.0))
        except Exception:
            return 0.0

    sorted_by_vol = sorted(results, key=_vol, reverse=True)

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

    _UNIVERSE_CACHE = {"ts": now, "tickers": picked}
    return picked


# ---------------- OPTION CHAIN CACHE ----------------

_OPTION_CACHE: Dict[str, Dict[str, Any]] = {}


def get_option_chain_cached(ticker: str) -> Optional[Dict[str, Any]]:
    """
    Get snapshot option chain for an underlying with a short cache.

    - Uses Polygon v3 snapshot options endpoint.
    - 120-second per-underlying cache.
    - On failure, returns last good cached value if available.
    """
    if not POLYGON_KEY:
        return None

    ticker = ticker.upper()
    now = time.time()
    cached = _OPTION_CACHE.get(ticker)
    if cached and now - cached.get("ts", 0) < 120:
        return cached.get("data")

    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] option chain fetch failed for {ticker}: {e}")
        return _OPTION_CACHE.get(ticker, {}).get("data")

    _OPTION_CACHE[ticker] = {"ts": now, "data": data}
    return data


def get_last_option_trades_cached(full_option_symbol: str) -> Any:
    """
    Get the last trade for a single option symbol with a short cache.

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

    url = f"https://api.polygon.io/v3/trades/{full_option_symbol}/last"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 404:
            print(f"[shared] last trade 404 for {full_option_symbol}")
            _OPTION_CACHE[key] = {"ts": now, "data": None}
            return None

        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] last trade fetch failed for {full_option_symbol}: {e}")
        return None

    _OPTION_CACHE[key] = {"ts": now, "data": data}
    return data


# ---------------- GRADER ----------------


def grade_equity_setup(move_pct: float, rvol: float, dollar_vol: float) -> str:
    """
    Rough A+/A/B/C grading based on move, RVOL, and dollar volume.

    This is intentionally simple — we just want a quick qualitative label
    for unusual / momentum setups in the alerts.
    """
    score = 0

    # Move contribution
    if move_pct >= 3:
        score += 1
    if move_pct >= 5:
        score += 1
    if move_pct >= 8:
        score += 1

    # RVOL contribution
    if rvol >= 2:
        score += 1
    if rvol >= 3:
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