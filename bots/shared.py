# bots/shared.py
import os
import time
import math
import json
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pytz
import requests

# ---------------- BASIC CONFIG ----------------

POLYGON_KEY = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")

# Global RVOL / volume floors that other bots can reference
# Made more aggressive:
#   • MIN_RVOL_GLOBAL: 2.0 → 1.3
#   • MIN_VOLUME_GLOBAL: 500k → 250k
MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "1.3"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "250000"))  # shares

# Telegram routing (your env)
# - TELEGRAM_CHAT_ALL = single private chat ID for everything
# - TELEGRAM_TOKEN_ALERTS = all-in-one alerts bot for all trade signals
# - TELEGRAM_TOKEN_STATUS = dedicated status/heartbeat bot
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

# Some bots may want a separate status-only chat; if not set, fallback to CHAT_ALL
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS") or TELEGRAM_CHAT_ALL

eastern = pytz.timezone("US/Eastern")


# ---------------- TIME HELPERS ----------------


def now_est() -> str:
    """Human-friendly time string in Eastern, e.g. '10:48 AM EST · Nov 20'."""
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
    so it shows up in the Telegram error digest even if the main bot continues.
    """
    ts = now_est()
    text = f"⚠️ [{bot}] {ts}\n{message}"
    _send_status(text)


# ---------------- DYNAMIC UNIVERSE ----------------


def get_dynamic_top_volume_universe(
    max_tickers: int = 150,
    volume_coverage: float = 0.90,
) -> List[str]:
    """
    Use Polygon's previous day's most-active (by dollar volume) to build a liquid universe.

    We try the v2 /aggs/grouped endpoint with sort by 'v' or 'otc' as needed.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot build dynamic universe.")
        return []

    today = today_est_date()
    prev = today - timedelta(days=1)
    from_ = prev.isoformat()
    to_ = prev.isoformat()

    url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{from_}"
    params = {"adjusted": "true", "apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        msg = f"[shared] error fetching dynamic universe: {e}"
        print(msg)
        report_status_error("shared:universe", msg)
        return []

    results = data.get("results") or []
    # each result: {T: 'AAPL', v: volume, vw: vwap, ...}
    enriched: List[Tuple[str, float, float]] = []
    for row in results:
        sym = row.get("T")
        vol = float(row.get("v") or 0.0)
        vwap = float(row.get("vw") or 0.0)
        dollar_vol = vol * vwap
        if not sym or dollar_vol <= 0:
            continue
        enriched.append((sym, vol, dollar_vol))

    if not enriched:
        print("[shared] dynamic universe: 0 names (no results from Polygon).")
        return []

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

    print(
        f"[shared] dynamic universe: {len(universe)} names, "
        f"covers ~{volume_coverage*100:.0f}% vol. "
        f"(global MIN_RVOL={MIN_RVOL_GLOBAL}, MIN_VOL={MIN_VOLUME_GLOBAL:,})"
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
    Uses v2 last trade + previous day's volume as an approximation.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last trade.")
        return None, None

    key = symbol.upper()
    now = time.time()
    entry = _LAST_TRADE_CACHE.get(key)
    if entry and isinstance(entry.ts, (int, float)) and now - float(entry.ts) < ttl_seconds:
        return entry.last, entry.dollar_vol

    # v2 last trade
    url = f"https://api.polygon.io/v2/last/trade/{symbol.upper()}"
    params = {"apiKey": POLYGON_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        msg = f"[shared] error fetching last trade for {symbol}: {e}"
        print(msg)
        report_status_error("shared:last_trade", msg)
        return None, None

    last_raw = (
        (data.get("results") or {}).get("p")
        if isinstance(data.get("results"), dict)
        else None
    )
    try:
        last_price = float(last_raw) if last_raw is not None else None
    except (TypeError, ValueError):
        last_price = None

    if last_price is None or last_price <= 0:
        return None, None

    # approximate dollar volume from previous day's volume * last price
    # (bots that need exact intraday RVOL will fetch their own aggs)
    dollar_vol = last_price * MIN_VOLUME_GLOBAL

    _LAST_TRADE_CACHE[key] = LastTradeCacheEntry(ts=now, last=last_price, dollar_vol=dollar_vol)
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
    ttl_seconds: int = 60,
) -> Optional[Dict[str, Any]]:
    """Fetches Polygon snapshot option chain via HTTP and caches it.

    Used by cheap / unusual / whales, etc.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch option chain.")
        return None

    key = _cache_key("chain", underlying.upper())
    now = time.time()

    entry = _OPTION_CACHE.get(key)
    # Be robust to any legacy / malformed cache entries
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now - float(entry.ts) < ttl_seconds:
            return entry.data

    url = f"https://api.polygon.io/v3/snapshot/options/{underlying.upper()}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        msg = f"[shared] error fetching option chain for {underlying}: {e}"
        print(msg)
        report_status_error("shared:option_chain", msg)
        return None

    _OPTION_CACHE[key] = OptionCacheEntry(ts=now, data=data)
    return data


def get_last_option_trades_cached(
    full_option_symbol: str,
    ttl_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    """Fetches the last option trade for a specific contract (v3 last/trade)."""
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last option trades.")
        return None

    key = _cache_key("last_trade", full_option_symbol)
    now = time.time()

    entry = _OPTION_CACHE.get(key)
    # Be robust to any legacy / malformed cache entries
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now - float(entry.ts) < ttl_seconds:
            return entry.data

    url = f"https://api.polygon.io/v3/last/trade/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        # Treat 404 (no data) as a normal, non-fatal condition
        if r.status_code == 404:
            # Benign: no last option trade exists yet for this contract.
            # Commented out to avoid log spam.
            # msg_404 = f"[shared] no last option trade for {full_option_symbol} (404)."
            # print(msg_404)
            return None

        r.raise_for_status()
        data = r.json()
    except Exception as e:
        msg = f"[shared] error fetching last option trade for {full_option_symbol}: {e}"
        print(msg)
        report_status_error("shared:last_option_trade", msg)
        return None

    _OPTION_CACHE[key] = OptionCacheEntry(ts=now, data=data)
    return data


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