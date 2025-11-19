# bots/shared.py — core config + Telegram helpers + dynamic universe
import os
import time
from datetime import datetime
from typing import List, Dict, Tuple

import pytz
import requests

# ---------- Time helpers ----------

eastern = pytz.timezone("US/Eastern")


def now_est() -> str:
    # Example: "07:08 PM EST · Nov 18"
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d")


# ---------- Global filters (tweak via ENV if needed) ----------

MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL = int(os.getenv("MIN_VOLUME_GLOBAL", "500000"))
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

# ---------- Environment variables ----------

POLYGON_KEY = os.getenv("POLYGON_KEY", "")

TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL", "")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS", "")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS", "")

# Alert throttle: minimum seconds between alerts for the same (bot, symbol)
ALERT_THROTTLE_SEC = int(os.getenv("ALERT_THROTTLE_SEC", "900"))  # default 15 minutes

# ---------- Emoji map per bot ----------

_EMOJI_MAP = {
    "premarket": "🌅",
    "volume": "📊",
    "gap": "🕳️",
    "orb": "📏",
    "squeeze": "🧨",
    "unusual": "🕵️",
    "cheap": "💸",
    "earnings": "📣",
    "momentum_reversal": "🔄",
}

# ---------- ETF blacklist ----------

_default_etfs = "SPY,QQQ,IWM,DIA,IEMG,XLK,XLF,XLV,XLY,XLP,XLE,XLB,XLU,SMH"
_ETF_ENV = os.getenv("ETF_BLACKLIST", _default_etfs)
ETF_BLACKLIST = {s.strip().upper() for s in _ETF_ENV.split(",") if s.strip()}

# ---------- Alert tracking ----------

_LAST_ALERT_SENT: Dict[Tuple[str, str], float] = {}
_ALERT_COUNTS: Dict[str, int] = {}


def is_etf_blacklisted(symbol: str) -> bool:
    """Return True if symbol is in the ETF blacklist."""
    return symbol.upper() in ETF_BLACKLIST


def _record_alert(bot_name: str, symbol: str) -> bool:
    """
    Returns True if we should send an alert (not throttled),
    False if it should be skipped due to throttle.
    """
    now_ts = time.time()
    key = (bot_name.lower(), symbol.upper())
    last_ts = _LAST_ALERT_SENT.get(key)
    if last_ts is not None and (now_ts - last_ts) < ALERT_THROTTLE_SEC:
        # Throttled
        print(f"THROTTLED [{bot_name}] {symbol}")
        return False

    _LAST_ALERT_SENT[key] = now_ts
    _ALERT_COUNTS[bot_name] = _ALERT_COUNTS.get(bot_name, 0) + 1
    return True


def get_alert_counts_snapshot(reset: bool = False) -> Dict[str, int]:
    """
    Snapshot of how many alerts each bot sent since last reset.
    If reset=True, clears counters after returning.
    """
    snap = dict(_ALERT_COUNTS)
    if reset:
        _ALERT_COUNTS.clear()
    return snap


# ---------- Helpers for grading & sentiment ----------

def grade_equity_setup(move_pct: float, rvol: float, dollar_volume: float) -> str:
    """
    Very simple grading for equity moves.

    Inputs:
      move_pct     - % move today
      rvol         - relative volume
      dollar_volume- price * shares traded

    Returns: "A+", "A", "B", or "C"
    """
    dv = dollar_volume

    if move_pct >= 15 and rvol >= 8 and dv >= 150_000_000:
        return "A+"
    if move_pct >= 10 and rvol >= 4 and dv >= 75_000_000:
        return "A"
    if move_pct >= 5 and rvol >= 2 and dv >= 25_000_000:
        return "B"
    return "C"


def describe_option_flow(side: str, dte: int, moneyness: float) -> str:
    """
    side: "C" or "P"
    dte: days to expiration
    moneyness: abs(strike - underlying) / underlying (0.10 = 10% from spot)
    """
    s = side.upper() if side else "?"

    if s == "C":
        direction = "Bullish calls"
    elif s == "P":
        direction = "Bearish puts / hedge"
    else:
        direction = "Mixed flow"

    # Classification based on DTE + moneyness
    if dte <= 1 and moneyness > 0.10:
        style = "lottery / speculative"
    elif dte <= 3 and moneyness <= 0.10:
        style = "short-term directional"
    elif dte <= 14:
        style = "swing positioning"
    else:
        style = "longer-dated positioning"

    return f"{direction}, {style}"


def chart_link(symbol: str) -> str:
    """
    Build a chart link for the symbol. Default is TradingView.
    Can be overridden with CHART_BASE env if you prefer another site.
    """
    base = os.getenv("CHART_BASE", "https://www.tradingview.com/chart/?symbol=")
    return f"{base}{symbol.upper()}"


# ---------- Telegram helpers ----------

def _pick_alert_token(bot_name: str) -> str:
    if TELEGRAM_TOKEN_ALERTS:
        return TELEGRAM_TOKEN_ALERTS
    if TELEGRAM_TOKEN_STATUS:
        return TELEGRAM_TOKEN_STATUS
    return ""


def send_alert(
    bot_name: str,
    symbol: str,
    last_price: float,
    rvol: float,
    extra: str = "",
) -> None:
    """
    Core Telegram send function used by all bots.

    Layout:

    🧨 SQUEEZE — `OLMA`
    🕒 07:08 PM EST · Nov 18
    💰 $20.14 · 📊 RVOL 87.9x
    ────────────
    ...strategy-specific block...
    """
    if not TELEGRAM_CHAT_ALL:
        print("ALERT SKIPPED: TELEGRAM_CHAT_ALL not set")
        return

    token = _pick_alert_token(bot_name)
    if not token:
        print("ALERT SKIPPED: no Telegram token configured (TELEGRAM_TOKEN_ALERTS)")
        return

    # Throttle per (bot, symbol)
    if not _record_alert(bot_name, symbol):
        return

    timestamp = now_est()
    title = bot_name.upper().replace("_", " ")
    emoji = _EMOJI_MAP.get(bot_name.lower(), "📈")

    header_line = f"{emoji} *{title}* — `{symbol}`"
    time_line = f"🕒 {timestamp}"

    price_line = f"💰 ${last_price:.2f}"
    if rvol > 0:
        price_line += f" · 📊 RVOL {rvol:.1f}x"

    msg = f"{header_line}\n{time_line}\n{price_line}\n────────────"

    if extra:
        msg += f"\n{extra}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"ALERT SENT [{bot_name}] {symbol}")
    except Exception as e:
        print(f"ALERT FAILED [{bot_name}] {symbol}: {e}")


def send_status_message(text: str) -> None:
    if not TELEGRAM_CHAT_ALL:
        return
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    if not token:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ALL, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def start_polygon_websocket():
    print("Polygon/WebSocket placeholder — using REST scanners only for now.")


# ---------- Dynamic top-volume universe ----------

DYNAMIC_UNIVERSE_REFRESH_SEC = int(os.getenv("DYNAMIC_UNIVERSE_REFRESH_SEC", "300"))

_dynamic_universe_cache = {
    "tickers": [],  # type: List[str]
    "ts": 0.0,
}


def get_dynamic_top_volume_universe(
    max_tickers: int = 100,
    volume_coverage: float = 0.90,
) -> List[str]:
    """
    Build a dynamic universe of liquid names that covers ~90% of total volume.
    """
    now_ts = time.time()
    if (
        _dynamic_universe_cache["tickers"]
        and (now_ts - _dynamic_universe_cache["ts"]) < DYNAMIC_UNIVERSE_REFRESH_SEC
    ):
        return _dynamic_universe_cache["tickers"]

    if not POLYGON_KEY:
        print("[universe] POLYGON_KEY not set; returning cached/static universe.")
        return _dynamic_universe_cache["tickers"] or []

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"

    try:
        resp = requests.get(url, params={"apiKey": POLYGON_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        raw_tickers = data.get("tickers", [])
    except Exception as e:
        print(f"[universe] Snapshot fetch failed: {e}")
        return _dynamic_universe_cache["tickers"] or []

    vols = []
    for t in raw_tickers:
        try:
            sym = t.get("ticker")
            day = t.get("day") or {}
            vol = float(day.get("v") or 0.0)
            if not sym or vol <= 0:
                continue
            vols.append((sym, vol))
        except Exception:
            continue

    if not vols:
        print("[universe] Snapshot contained no usable tickers.")
        return _dynamic_universe_cache["tickers"] or []

    vols.sort(key=lambda x: x[1], reverse=True)
    total_vol = float(sum(v for _, v in vols))
    selected: List[str] = []
    cumulative = 0.0

    for sym, vol in vols:
        selected.append(sym)
        cumulative += vol

        if len(selected) >= max_tickers:
            break

        if total_vol > 0 and (cumulative / total_vol) >= volume_coverage:
            break

    _dynamic_universe_cache["tickers"] = selected
    _dynamic_universe_cache["ts"] = now_ts

    coverage_pct = (cumulative / total_vol * 100.0) if total_vol else 0.0
    print(f"[universe] Selected {len(selected)} tickers covering {coverage_pct:.1f}% of volume.")

    return selected