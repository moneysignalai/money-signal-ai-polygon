# bots/panic_flush.py

import os
from datetime import date, timedelta, datetime
from typing import List

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

MIN_PRICE = float(os.getenv("PANIC_MIN_PRICE", "5.0"))
MAX_PRICE = float(os.getenv("PANIC_MAX_PRICE", "200.0"))
MIN_DOLLAR_VOL = float(os.getenv("PANIC_MIN_DOLLAR_VOL", "30000000"))  # $30M+
MIN_RVOL = float(os.getenv("PANIC_MIN_RVOL", "2.0"))
MIN_DOWN_PCT = float(os.getenv("PANIC_MIN_DOWN_PCT", "5.0"))
NEAR_LOW_PCT = float(os.getenv("PANIC_NEAR_LOW_PCT", "5.0"))  # within 5% of 52w low
LOOKBACK_DAYS = int(os.getenv("PANIC_LOOKBACK_DAYS", "252"))  # ~52w

# ------------------- STATE -------------------

_alert_date: date | None = None
_alerted: set[str] = set()


def _reset_if_new_day():
    global _alert_date, _alerted
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted = set()


def _already(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted


def _mark(sym: str):
    _reset_if_new_day()
    _alerted.add(sym)


def _in_rth() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60  # 09:30–16:00 ET


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.97)


# ------------------- MAIN BOT -------------------


async def run_panic_flush():
    """
    Panic Flush Bot — capitulation into 52-week lows.

      • Time: RTH only (09:30–16:00 ET)
      • Universe: TICKER_UNIVERSE env OR dynamic top volume universe (200 names)
      • Filters:
          - Price between MIN_PRICE and MAX_PRICE
          - Day $ volume ≥ MIN_DOLLAR_VOL
          - RVOL ≥ max(MIN_RVOL_GLOBAL, MIN_RVOL)
          - Down ≥ MIN_DOWN_PCT% on day
          - Making a new 30-day low
          - Within NEAR_LOW_PCT% of 52-week low
    """
    if not POLYGON_KEY or not _client:
        print("[panic_flush] Missing client/API key.")
        return
    if not _in_rth():
        print("[panic_flush] Outside RTH; skipping.")
        return

    _reset_if_new_day()
    universe = _universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        # --- Fetch daily data ---
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=LOOKBACK_DAYS)).isoformat(),
                    to=today_s,
                    limit=LOOKBACK_DAYS,
                )
            )
        except Exception as e:
            print(f"[panic_flush] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 40:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        try:
            last_price = float(today_bar.close)
            prev_close = float(prev_bar.close)
            day_vol = float(today_bar.volume or 0.0)
            day_low = float(today_bar.low)
        except Exception:
            continue

        if last_price < MIN_PRICE or last_price > MAX_PRICE:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < max(MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
            continue

        # --- RVOL ---
        vols = [float(d.volume or 0.0) for d in days[-21:-1]]
        avg_vol = sum(vols) / max(len(vols), 1)
        if avg_vol <= 0:
            continue
        rvol = day_vol / avg_vol
        if rvol < max(MIN_RVOL_GLOBAL, MIN_RVOL):
            continue

        # Day move vs yesterday
        if prev_close <= 0:
            continue
        move_pct = (last_price / prev_close - 1.0) * 100.0
        if move_pct > -MIN_DOWN_PCT:
            continue  # not flushed enough

        # 52-week low
        lows_52w = [float(d.low) for d in days]
        low_52w = min(lows_52w)
        if low_52w <= 0:
            continue

        distance_from_low_pct = (last_price - low_52w) / low_52w * 100.0
        if distance_from_low_pct > NEAR_LOW_PCT:
            continue

        # Today's low must be a new 30-day low
        last_30 = days[-31:-1]
        if last_30:
            min_last_30 = min(float(d.low) for d in last_30)
            if day_low > min_last_30:
                continue

        grade = grade_equity_setup(move_pct, rvol, dollar_vol)

        # --- Alert formatting (A-style) ---
        timestamp = datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")
        emoji = "💥"
        skull_emoji = "☠️"
        money_emoji = "💰"
        divider = "────────────"

        extra = (
            f"{emoji} PANIC FLUSH — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{skull_emoji} Down {abs(move_pct):.1f}% today\n"
            f"📉 New 30-day low today near 52-week low\n"
            f"📉 52w low: ${low_52w:.2f} (dist {distance_from_low_pct:.1f}%)\n"
            f"📦 Volume: {int(day_vol):,} · Dollar Vol ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: PANIC ZONE\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("panic_flush", sym, last_price, rvol, extra=extra)
