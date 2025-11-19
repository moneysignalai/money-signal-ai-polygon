# bots/shared.py — core config + Telegram helpers + dynamic universe
import os
import time
from datetime import datetime
from typing import List

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

    timestamp = now_est()
    title = bot_name.upper().replace("_", " ")
    emoji = _EMOJI_MAP.get(bot_name.lower(), "📈")

    # Line 1: strategy + symbol (short so it doesn't wrap)
    header_line = f"{emoji} *{title}* — `{symbol}`"

    # Line 2: time
    time_line = f"🕒 {timestamp}"

    # Line 3: price + optional RVOL
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
