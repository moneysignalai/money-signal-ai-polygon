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
MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "500000"))  # shares

# Telegram routing (your env)
# - TELEGRAM_CHAT_ALL = single private chat ID for everything
# - TELEGRAM_TOKEN_ALERTS = all-in-one alerts bot for all trade signals
# - TELEGRAM_TOKEN_STATUS = dedicated status/heartbeat bot
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")

TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

eastern = pytz.timezone("US/Eastern")


def now_est() -> datetime:
    return datetime.now(eastern)


# ---------------- TELEGRAM LOW-LEVEL ----------------


def _send_telegram_raw(
    token: Optional[str],
    chat_id: Optional[str],
    text: str,
    parse_mode: Optional[str] = "Markdown",
) -> None:
    """Low-level Telegram sender used by alerts + status.

    Safe: prints errors but never raises, so one bad message cannot crash the app.
    """
    if not token or not chat_id:
        print(f"[telegram] missing token/chat_id, skipping message: {text[:80]}")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        r = requests.post(url, json=payload, timeout=10)
        if not r.ok:
            print(f"[telegram] send failed {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[telegram] exception while sending message: {e}")


# ---------------- PUBLIC ALERT SENDER ----------------


def send_alert(
    bot_name: str,
    symbol: str,
    price: float,
    rvol: float | None = None,
    extra: str | None = None,
) -> None:
    """Main alert pipe for all trade bots.

    Uses:
      - TELEGRAM_TOKEN_ALERTS
      - TELEGRAM_CHAT_ALL
    """
    token = TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_ALL

    if not token or not chat:
        print(f"[alert:{bot_name}] (no TELEGRAM_TOKEN_ALERTS or TELEGRAM_CHAT_ALL) {symbol} {price}")
        if extra:
            print(extra)
        return

    # Basic header line; bots are expected to supply a fully formatted 'extra' body
    header = f"[{bot_name.upper()}] {symbol} @ ${price:.2f}"
    if rvol is not None:
        header += f" · RVOL {rvol:.1f}x"

    if extra:
        msg = f"{extra}"
    else:
        msg = header

    _send_telegram_raw(token, chat, msg, parse_mode="Markdown")


def send_status(message: str) -> None:
    """Status / heartbeat / error digest messages.

    Uses ONLY:
      - TELEGRAM_TOKEN_STATUS (dedicated status bot)
      - TELEGRAM_CHAT_ALL (same private chat as alerts)
      - NO Markdown (plain text), so raw errors / stack traces cannot break it.

    If TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL is missing,
    we log the status line to stdout and skip Telegram.
    """
    token = TELEGRAM_TOKEN_STATUS
    chat = TELEGRAM_CHAT_ALL

    if not token or not chat:
        print(f"[status] (no TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL) {message}")
        return

    # Plain text: avoids 400 "can't parse entities" errors from Telegram
    _send_telegram_raw(token, chat, message, parse_mode=None)


def report_status_error(bot: str, message: str) -> None:
    """Bridge helper so shared/bots can push errors into status_report.

    This keeps status reporting centralized without creating an import cycle.
    Called by shared helpers when a *soft* error happens (e.g. Polygon fails),
    so it shows up in the Telegram error digest even if the bot doesn't crash.
    """
    try:
        from bots import status_report  # type: ignore

        if hasattr(status_report, "record_bot_error"):
            status_report.record_bot_error(bot, message)
    except Exception as e:
        # Never raise from here; last resort is just console logging
        print(f"[shared] failed to forward error to status_report: {e}; original={message}")


# ---------------- ETF BLACKLIST ----------------

# Basic ETF blacklist; can be extended via ENV if you want
_DEFAULT_ETF_BLACKLIST = {
    "SPY",
    "QQQ",
    "IWM",
    "UVXY",
    "SVXY",
    "SQQQ",
    "TQQQ",
    "SPXU",
    "XLK",
    "XLF",
    "XLE",
    "XLP",
    "XLV",
    "XLY",
    "XLI",
    "XLB",
    "XLU",
    "ARKK",
}

_ETF_EXTRA = set(
    x.strip().upper()
    for x in os.getenv("ETF_BLACKLIST_EXTRA", "").split(",")
    if x.strip()
)

ETF_BLACKLIST = _DEFAULT_ETF_BLACKLIST | _ETF_EXTRA


def is_etf_blacklisted(symbol: str) -> bool:
    return symbol.upper() in ETF_BLACKLIST


# ---------------- DYNAMIC UNIVERSE (TOP VOLUME) ----------------


@dataclass
class TickerRecord:
    ticker: str
    volume: float
    dollar_volume: float


_UNIVERSE_CACHE: Dict[str, Any] = {
    "ts": None,
    "tickers": [],
}


def get_dynamic_top_volume_universe(
    max_tickers: int = 200,
    volume_coverage: float = 0.97,
) -> List[str]:
    """Fetch a dynamic ticker universe based on top dollar-volume.

    Cached for a few minutes to avoid hammering Polygon.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; dynamic universe fallback = empty list.")
        return []

    now = time.time()
    ts = _UNIVERSE_CACHE.get("ts")
    if ts and now - ts < 180:
        return list(_UNIVERSE_CACHE.get("tickers", []))

    url = "https://api.polygon.io/v3/snapshot/stocks"
    params = {"apiKey": POLYGON_KEY, "limit": 1000}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        msg = f"[shared] error fetching dynamic universe: {e}"
        print(msg)
        report_status_error("shared:universe", msg)
        return _UNIVERSE_CACHE["tickers"]

    results = data.get("tickers") or []
    if not results:
        msg = "[shared] no tickers in dynamic universe response."
        print(msg)
        report_status_error("shared:universe", msg)
        return _UNIVERSE_CACHE["tickers"]

    def _vol(rec: Dict[str, Any]) -> float:
        try:
            return float(rec.get("todaysVolume") or 0.0)
        except Exception:
            return 0.0

    def _dollar_vol(rec: Dict[str, Any]) -> float:
        try:
            c = float(rec.get("day", {}).get("c") or 0.0)
            v = float(rec.get("day", {}).get("v") or 0.0)
            return c * v
        except Exception:
            return 0.0

    records: List[TickerRecord] = []
    for rec in results:
        sym = rec.get("ticker")
        if not sym:
            continue
        dv = _dollar_vol(rec)
        if dv <= 0:
            continue
        records.append(
            TickerRecord(
                ticker=sym,
                volume=_vol(rec),
                dollar_volume=dv,
            )
        )

    records.sort(key=lambda r: r.dollar_volume, reverse=True)
    if not records:
        print("[shared] dynamic universe produced no valid records.")
        return []

    total_dv = sum(r.dollar_volume for r in records)
    running = 0.0
    chosen: List[str] = []

    for r in records:
        chosen.append(r.ticker)
        running += r.dollar_volume
        if running / total_dv >= volume_coverage or len(chosen) >= max_tickers:
            break

    _UNIVERSE_CACHE["ts"] = now
    _UNIVERSE_CACHE["tickers"] = chosen
    print(f"[shared] dynamic universe size={len(chosen)} coverage≈{running/total_dv:.1%}")
    return chosen


# ---------------- OPTION SNAPSHOT + LAST TRADE (CACHED) ----------------

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
    if entry and now - entry.ts < ttl_seconds:
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
    if entry and now - entry.ts < ttl_seconds:
        return entry.data

    url = f"https://api.polygon.io/v3/last/trade/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        # Treat 404 (no data) as a normal, non-fatal condition
        if r.status_code == 404:
            msg_404 = f"[shared] no last option trade for {full_option_symbol} (404)."
            print(msg_404)
            # 404 is benign; we do not spam status bot with it
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


# ---- Backwards-compat function names (old bots) ----

def getOptionChainCached(underlying: str, ttl_seconds: int = 60):
    """Legacy camelCase alias used by older bots."""
    return get_option_chain_cached(underlying, ttl_seconds=ttl_seconds)


def getLastOptionTradesCached(full_option_symbol: str, ttl_seconds: int = 30):
    """Legacy camelCase alias used by older bots."""
    return get_last_option_trades_cached(full_option_symbol, ttl_seconds=ttl_seconds)


# ---------------- CHART LINK ----------------


def chart_link(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={symbol.upper()}"


# ---------------- GRADING HELPERS ----------------


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
    tz=eastern,
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