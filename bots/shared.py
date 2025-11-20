# bots/shared.py — MoneySignalAI shared helpers (with 3-minute universe cache)

import os
import math
from datetime import datetime, timedelta
from typing import List

import requests
import pytz

# ---------------- TIME / ENV ----------------

eastern = pytz.timezone("US/Eastern")


def now_est() -> str:
    """Formatted timestamp in EST for alerts/status."""
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# Core ENV
POLYGON_KEY = os.getenv("POLYGON_KEY", "")

# Global filters (can be overridden in Render ENV)
MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.5"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "800000"))

# Telegram
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS", "")
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL", "")

TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS", "")
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS", "")  # optional, else falls back to CHAT_ALL

TELEGRAM_API_BASE = "https://api.telegram.org"


# ---------------- TELEGRAM HELPERS ----------------

def _send_telegram_raw(token: str, chat_id: str, text: str) -> None:
    """Low-level Telegram send; prints to console if misconfigured."""
    if not token or not chat_id:
        print("[telegram] missing token/chat_id — dumping message:")
        print(text)
        return

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if not resp.ok:
            print(f"[telegram] send failed {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[telegram] exception: {e}")


def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    """
    Main alert function used by all bots.

    • If `extra` is provided, it's treated as the FULL message (we don't wrap it).
    • If `extra` is empty, we send a simple generic header.
    """
    if extra:
        text = extra
    else:
        header = f"🔔 {bot_name.upper()} — {ticker}\n"
        meta = f"💰 ${price:.2f} · RVOL {rvol:.1f}x\n"
        text = header + meta

    _send_telegram_raw(TELEGRAM_TOKEN_ALERTS, TELEGRAM_CHAT_ALL, text)


def send_status(message: str) -> None:
    """Optional: send status/health pings via TELEGRAM_TOKEN_STATUS."""
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    chat_id = TELEGRAM_CHAT_STATUS or TELEGRAM_CHAT_ALL

    if not token or not chat_id:
        print("[status] missing token/chat_id, printing message:")
        print(message)
        return

    _send_telegram_raw(token, chat_id, message)


# ---------------- UTILITIES / UNIVERSE ----------------

def chart_link(ticker: str) -> str:
    """TradingView chart link."""
    return f"https://www.tradingview.com/chart/?symbol={ticker.upper()}"


# Simple ETF/index blacklist so some bots stay focused on single names
ETF_BLACKLIST = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "VXX",
    "UVXY",
    "SPXL",
    "SPXS",
    "SQQQ",
    "TQQQ",
}


def is_etf_blacklisted(ticker: str) -> bool:
    return ticker.upper() in ETF_BLACKLIST


# ---- Universe cache (in-memory, per process) ----

_UNIVERSE_CACHE: List[str] | None = None
_UNIVERSE_TS: datetime | None = None
_UNIVERSE_TTL_SECONDS = int(os.getenv("UNIVERSE_CACHE_TTL", "180"))  # 3 minutes default


def get_dynamic_top_volume_universe(
    max_tickers: int = 150,
    volume_coverage: float = 0.95,
) -> List[str]:
    """
    Dynamic universe builder (with 3-minute cache).

    Priority:
      1) If TICKER_UNIVERSE is set in ENV, use that list directly.
      2) Else, if cached universe is younger than UNIVERSE_CACHE_TTL seconds, reuse it.
      3) Else, call Polygon's snapshot endpoint:
         /v2/snapshot/locale/us/markets/stocks/tickers
         and sort by daily volume descending.

    We then pick tickers until we either:
      • hit `max_tickers`, OR
      • reach `volume_coverage` of total reported volume.
    """
    global _UNIVERSE_CACHE, _UNIVERSE_TS

    # 1) Manual override wins
    manual = os.getenv("TICKER_UNIVERSE")
    if manual:
        tickers = [x.strip().upper() for x in manual.split(",") if x.strip()]
        print(f"[shared] using TICKER_UNIVERSE override ({len(tickers)} tickers).")
        return tickers

    # 2) Cache check
    now = datetime.utcnow()
    if _UNIVERSE_CACHE is not None and _UNIVERSE_TS is not None:
        age = (now - _UNIVERSE_TS).total_seconds()
        if age <= _UNIVERSE_TTL_SECONDS:
            # cached universe is still fresh
            print(f"[shared] using cached universe ({len(_UNIVERSE_CACHE)} tickers, age {int(age)}s).")
            return _UNIVERSE_CACHE

    # 3) Fetch from Polygon
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; universe fallback empty.")
        _UNIVERSE_CACHE = []
        _UNIVERSE_TS = now
        return []

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"apiKey": POLYGON_KEY}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[shared] universe fetch failed: {e}")
        # on failure, keep old cache if it exists
        if _UNIVERSE_CACHE is not None:
            print("[shared] falling back to previous cached universe.")
            return _UNIVERSE_CACHE
        return []

    tickers_data = data.get("tickers", []) or []

    volumes = []
    for t in tickers_data:
        ticker = t.get("ticker")
        day = t.get("day", {}) or {}
        vol = day.get("v", 0) or 0

        if not ticker:
            continue

        volumes.append((ticker, vol))

    if not volumes:
        print("[shared] universe: no tickers found in snapshot.")
        _UNIVERSE_CACHE = []
        _UNIVERSE_TS = now
        return []

    # Sort by volume descending
    volumes.sort(key=lambda x: x[1], reverse=True)

    total_volume = sum(v for _, v in volumes)
    chosen: List[str] = []
    running = 0

    for ticker, vol in volumes:
        chosen.append(ticker)
        running += vol

        if len(chosen) >= max_tickers:
            break
        if total_volume > 0 and (running / total_volume) >= volume_coverage:
            break

    coverage_pct = (running / total_volume * 100) if total_volume else 0.0
    print(
        f"[shared] universe built from Polygon: {len(chosen)} tickers, "
        f"{running:,} / {total_volume:,} volume ({coverage_pct:.1f}%)."
    )

    _UNIVERSE_CACHE = chosen
    _UNIVERSE_TS = now
    return chosen


# ---------------- GRADING / SCORING ----------------

def grade_equity_setup(move_pct: float, rvol: float, dollar_vol: float) -> str:
    """
    Rough letter grade for equity setups.

    • move_pct: abs(% move today)
    • rvol: relative volume (current / avg)
    • dollar_vol: price * volume (approx)

    Returns: "A+", "A", "B", or "C"
    """
    score = 0

    # Move component
    if move_pct >= 3:
        score += 1
    if move_pct >= 7:
        score += 1
    if move_pct >= 12:
        score += 1

    # RVOL component
    if rvol >= 2:
        score += 1
    if rvol >= 4:
        score += 1

    # Dollar volume component
    if dollar_vol >= 25_000_000:
        score += 1
    if dollar_vol >= 75_000_000:
        score += 1
    if dollar_vol >= 200_000_000:
        score += 1

    if score >= 6:
        return "A+"
    elif score >= 4:
        return "A"
    elif score >= 2:
        return "B"
    else:
        return "C"