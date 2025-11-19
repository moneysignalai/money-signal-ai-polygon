# bots/trend_rider.py

import os
from datetime import date, timedelta, datetime
from typing import List, Any
import pytz
import math

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

MIN_PRICE = float(os.getenv("TREND_MIN_PRICE", "10.0"))
BREAKOUT_LOOKBACK = int(os.getenv("TREND_BREAKOUT_LOOKBACK", "20"))
BREAKOUT_MIN_PCT = float(os.getenv("TREND_BREAKOUT_MIN_PCT", "1.0"))  # % past range
MIN_TREND_RVOL = float(os.getenv("TREND_MIN_RVOL", "2.0"))

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


def _in_trend_window() -> bool:
    """
    Run from 15:30–20:00 ET to catch near-EOD and post-close daily bars.
    """
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 15 * 60 + 30 <= mins <= 20 * 60


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.97)


def _ema(values, period: int) -> float:
    if not values or len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2 / (period + 1.0)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


async def run_trend_rider():
    """
    TrendRider Bot:

      • Uptrend breakout:
          - 20 EMA > 50 EMA
          - Close > prior N-day high by BREAKOUT_MIN_PCT
      • Downtrend breakdown:
          - 20 EMA < 50 EMA
          - Close < prior N-day low by BREAKOUT_MIN_PCT
      • Filters:
          - Price >= MIN_PRICE
          - RVOL >= max(MIN_TREND_RVOL, MIN_RVOL_GLOBAL)
          - Volume >= MIN_VOLUME_GLOBAL
    """
    if not POLYGON_KEY or not _client:
        print("[trend_rider] Missing client/API key.")
        return
    if not _in_trend_window():
        print("[trend_rider] Outside trend window; skipping.")
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
                    from_=(today - timedelta(days=120)).isoformat(),
                    to=today_s,
                    limit=120,
                )
            )
        except Exception as e:
            print(f"[trend_rider] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 60:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        closes = [float(d.close) for d in days]
        highs = [float(d.high) for d in days]
        lows = [float(d.low) for d in days]

        last_price = closes[-1]
        prev_close = closes[-2]

        if last_price < MIN_PRICE:
            continue

        # EMAs for trend
        ema20 = _ema(closes[-60:], 20)
        ema50 = _ema(closes[-60:], 50)

        # RVOL
        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(d.volume for d in recent) / len(recent)
        day_vol = float(today_bar.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        if rvol < max(MIN_TREND_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        # Breakout levels
        lookback_slice = days[-(BREAKOUT_LOOKBACK + 1):-1]
        if len(lookback_slice) < BREAKOUT_LOOKBACK:
            continue
        lb_high = max(float(d.high) for d in lookback_slice)
        lb_low = min(float(d.low) for d in lookback_slice)

        breakout_pct_above = (last_price - lb_high) / lb_high * 100.0 if lb_high > 0 else 0.0
        breakout_pct_below = (lb_low - last_price) / lb_low * 100.0 if lb_low > 0 else 0.0

        # Decide if this is an uptrend breakout or downtrend breakdown
        uptrend = ema20 > ema50
        downtrend = ema20 < ema50

        direction = None
        reason = None

        if uptrend and breakout_pct_above >= BREAKOUT_MIN_PCT:
            direction = "LONG"
            reason = f"Breakout above {BREAKOUT_LOOKBACK}-day high by {breakout_pct_above:.1f}%"
        elif downtrend and breakout_pct_below >= BREAKOUT_MIN_PCT:
            direction = "SHORT"
            reason = f"Breakdown below {BREAKOUT_LOOKBACK}-day low by {breakout_pct_below:.1f}%"
        else:
            continue

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        emoji = "📈" if direction == "LONG" else "📉"
        trend_emoji = "📊"
        money_emoji = "💰"
        divider = "────────────"
        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"{emoji} TREND RIDER — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{trend_emoji} {reason}\n"
            f"📈 20 EMA: {ema20:.2f} · 50 EMA: {ema50:.2f}\n"
            f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: {direction}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("trend_rider", sym, last_price, rvol, extra=extra)