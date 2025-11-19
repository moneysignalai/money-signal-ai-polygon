# bots/swing_pullback.py

import os
from datetime import date, timedelta, datetime
from typing import List, Any
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

MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_DISTANCE_FROM_EMA = float(os.getenv("PULLBACK_MAX_DIST_EMA", "2.0"))  # % from 20 EMA
MIN_PULLBACK_RVOL = float(os.getenv("PULLBACK_MIN_RVOL", "1.5"))

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


def _ema(values, period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1.0)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


async def run_swing_pullback():
    """
    Swing Pullback Bot:

      • Find strong uptrends (20 EMA > 50 EMA).
      • Price pulls back toward 20 EMA and prints a green candle.
      • Distance from 20 EMA <= MAX_DISTANCE_FROM_EMA.
      • RVOL >= max(MIN_PULLBACK_RVOL, MIN_RVOL_GLOBAL).
    """
    if not POLYGON_KEY or not _client:
        print("[swing_pullback] Missing client/API key.")
        return
    if not _in_rth():
        print("[swing_pullback] Outside RTH; skipping.")
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
                    from_=(today - timedelta(days=90)).isoformat(),
                    to=today_s,
                    limit=90,
                )
            )
        except Exception as e:
            print(f"[swing_pullback] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 40:
            continue

        closes = [float(d.close) for d in days]
        last_bar = days[-1]
        prev_bar = days[-2]

        last_price = closes[-1]
        prev_close = closes[-2]

        if last_price < MIN_PRICE:
            continue

        ema20 = _ema(closes[-40:], 20)
        ema50 = _ema(closes[-40:], 50)

        if ema20 <= ema50:
            # not a strong uptrend
            continue

        # RVOL
        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(d.volume for d in recent) / len(recent)
        day_vol = float(last_bar.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        if rvol < max(MIN_PULLBACK_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        # Green candle and near EMA20
        open_today = float(last_bar.open)
        if last_price <= open_today:
            # not a green candle
            continue

        dist_from_ema_pct = abs(last_price - ema20) / last_price * 100.0
        if dist_from_ema_pct > MAX_DISTANCE_FROM_EMA:
            continue

        # Basic pullback check: last few days had some red
        recent_closes = closes[-6:-1]
        red_days = sum(1 for i in range(1, len(recent_closes)) if recent_closes[i] < recent_closes[i - 1])
        if red_days < 2:
            # not a meaningful pullback
            continue

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        emoji = "🔄"
        money_emoji = "💰"
        trend_emoji = "📊"
        divider = "────────────"
        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"{emoji} SWING PULLBACK — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"📌 Strong uptrend: 20 EMA {ema20:.2f} > 50 EMA {ema50:.2f}\n"
            f"📉 Recent pullback with {red_days} red days\n"
            f"📍 Close near 20 EMA (dist {dist_from_ema_pct:.1f}%)\n"
            f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("swing_pullback", sym, last_price, rvol, extra=extra)