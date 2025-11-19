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

MIN_PRICE = float(os.getenv("PANIC_MIN_PRICE", "5.0"))
MIN_DROP_PCT = float(os.getenv("PANIC_MIN_DROP_PCT", "10.0"))  # -10%+
MIN_PANIC_RVOL = float(os.getenv("PANIC_MIN_RVOL", "3.0"))
MIN_DOLLAR_VOL = float(os.getenv("PANIC_MIN_DOLLAR_VOL", "20000000"))  # $20M
NEAR_LOW_PCT = float(os.getenv("PANIC_NEAR_LOW_PCT", "3.0"))  # within 3% of 52W low

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
    return 9 * 60 + 30 <= mins <= 16 * 60


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.97)


async def run_panic_flush():
    """
    Panic Flush Bot:

      • Price >= MIN_PRICE
      • Day move <= -MIN_DROP_PCT
      • RVOL >= max(MIN_PANIC_RVOL, MIN_RVOL_GLOBAL)
      • Dollar volume >= MIN_DOLLAR_VOL
      • Close within NEAR_LOW_PCT of 52-week low.
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

        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=260)).isoformat(),
                    to=today_s,
                    limit=260,
                )
            )
        except Exception as e:
            print(f"[panic_flush] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 30:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        last_price = float(today_bar.close)
        prev_close = float(prev_bar.close)

        if last_price < MIN_PRICE:
            continue

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )
        if move_pct > -MIN_DROP_PCT:
            # not a big enough flush
            continue

        # RVOL
        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(d.volume for d in recent) / len(recent)
        day_vol = float(today_bar.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        if rvol < max(MIN_PANIC_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_DOLLAR_VOL:
            continue

        # 52-week low proximity
        lows = [float(d.low) for d in days]
        low_52w = min(lows)
        if low_52w <= 0:
            continue

        distance_from_low_pct = (last_price - low_52w) / low_52w * 100.0
        if distance_from_low_pct > NEAR_LOW_PCT:
            # not close enough to the 52-week low
            continue

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        emoji = "💥"
        skull_emoji = "☠️"
        money_emoji = "💰"
        divider = "────────────"
        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"{emoji} PANIC FLUSH — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{skull_emoji} Down {abs(move_pct):.1f}% today\n"
            f"📉 Near 52-week low: last ${last_price:.2f} vs low ${low_52w:.2f} "
            f"(dist {distance_from_low_pct:.1f}%)\n"
            f"📦 Volume: {int(day_vol):,} · Dollar Vol ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: PANIC ZONE (bounce or short-continuation)\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("panic_flush", sym, last_price, rvol, extra=extra)