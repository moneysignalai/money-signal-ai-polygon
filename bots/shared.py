# bots/shared.py — MoneySignalAI (Universe cache + Option-chain cache)

import os
import math
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
import pytz

# ---------------- TIME / ENV ----------------

eastern = pytz.timezone("US/Eastern")


def now_est() -> str:
    """Formatted timestamp in EST for alerts/status."""
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ---------------- ENV ----------------

POLYGON_KEY = os.getenv("POLYGON_KEY", "")

MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.5"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "800000"))

TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS", "")
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL", "")

TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS", "")
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS", "")

TELEGRAM_API_BASE = "https://api.telegram.org"


# ---------------- TELEGRAM ----------------

def _send_telegram_raw(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        print("[telegram] Missing token/chat_id\n" + text)
        return

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }

    try:
        r = requests.post(url, data=payload, timeout=10)
        if not r.ok:
            print(f"[telegram] send failed: {r.status_code} — {r.text}")
    except Exception as e:
        print(f"[telegram] exception: {e}")


def send_alert(bot: str, ticker: str, price: float, rvol: float, extra=""):
    if extra:
        msg = extra
    else:
        msg = (
            f"🔔 *{bot.upper()} — {ticker}*\n"
            f"💰 ${price:.2f} · RVOL {rvol:.1f}x"
        )

    _send_telegram_raw(TELEGRAM_TOKEN_ALERTS, TELEGRAM_CHAT_ALL, msg)


def send_status(message: str):
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_STATUS or TELEGRAM_CHAT_ALL
    _send_telegram_raw(token, chat, message)


# ---------------- UTILITIES ----------------

def chart_link(ticker: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={ticker.upper()}"


ETF_BLACKLIST = {
    "SPY", "QQQ", "IWM", "DIA",
    "VXX", "UVXY", "SPXL", "SPXS",
    "SQQQ", "TQQQ"
}

def is_etf_blacklisted(ticker: str) -> bool:
    return ticker.upper() in ETF_BLACKLIST


# ---------------- UNIVERSE CACHE (3 min) ----------------

_UNIVERSE_CACHE = None
_UNIVERSE_TS = None
_UNIVERSE_TTL = int(os.getenv("UNIVERSE_CACHE_TTL", "180"))


def get_dynamic_top_volume_universe(
    max_tickers=150, volume_coverage=0.95
) -> List[str]:
    global _UNIVERSE_CACHE, _UNIVERSE_TS

    # manual override
    manual = os.getenv("TICKER_UNIVERSE")
    if manual:
        return [x.strip().upper() for x in manual.split(",")]

    now = datetime.utcnow()
    if _UNIVERSE_CACHE and _UNIVERSE_TS:
        age = (now - _UNIVERSE_TS).total_seconds()
        if age < _UNIVERSE_TTL:
            return _UNIVERSE_CACHE

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        tickers = r.json().get("tickers", [])
    except Exception as e:
        print("[shared] universe fetch failed:", e)
        return _UNIVERSE_CACHE or []

    volume_list = []
    for t in tickers:
        ticker = t.get("ticker")
        v = t.get("day", {}).get("v", 0)
        if not ticker:
            continue
        volume_list.append((ticker, v))

    volume_list.sort(key=lambda x: x[1], reverse=True)
    total_vol = sum(v for _, v in volume_list)

    picked = []
    running = 0

    for sym, v in volume_list:
        picked.append(sym)
        running += v
        if len(picked) >= max_tickers:
            break
        if total_vol > 0 and running / total_vol >= volume_coverage:
            break

    _UNIVERSE_CACHE = picked
    _UNIVERSE_TS = now

    return picked


# ---------------- OPTION CHAIN CACHE (NEW) ----------------

_OPTION_CACHE: Dict[str, Any] = {}
_OPTION_CACHE_TTL = 120  # 2 minutes


def get_option_chain_cached(ticker: str) -> Any:
    """
    Fetches full option chain for a symbol with caching to avoid
    hitting Polygon 10+ times per minute.
    """
    now = time.time()

    if ticker in _OPTION_CACHE:
        entry = _OPTION_CACHE[ticker]
        if now - entry["ts"] < _OPTION_CACHE_TTL:
            return entry["data"]

    url = f"https://api.polygon.io/v3/reference/options/contracts"
    params = {"underlying_ticker": ticker, "limit": 1000, "apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] option chain fetch failed for {ticker}: {e}")
        return _OPTION_CACHE.get(ticker, {}).get("data", None)

    _OPTION_CACHE[ticker] = {"ts": now, "data": data}
    return data


def get_last_option_trades_cached(full_option_symbol: str) -> Any:
    """Gets last trade for an option with 30-sec cache."""
    key = f"trade_{full_option_symbol}"

    now = time.time()

    if key in _OPTION_CACHE:
        entry = _OPTION_CACHE[key]
        if now - entry["ts"] < 30:
            return entry["data"]

    url = f"https://api.polygon.io/v2/last/trade/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[shared] last option trade failed for {full_option_symbol}: {e}")
        return None

    _OPTION_CACHE[key] = {"ts": now, "data": data}
    return data


# ---------------- GRADER ----------------

def grade_equity_setup(move_pct: float, rvol: float, dollar_vol: float) -> str:
    score = 0

    if move_pct >= 3: score += 1
    if move_pct >= 7: score += 1
    if move_pct >= 12: score += 1

    if rvol >= 2: score += 1
    if rvol >= 4: score += 1

    if dollar_vol >= 25_000_000: score += 1
    if dollar_vol >= 75_000_000: score += 1
    if dollar_vol >= 200_000_000: score += 1

    if score >= 6: return "A+"
    if score >= 4: return "A"
    if score >= 2: return "B"
    return "C"