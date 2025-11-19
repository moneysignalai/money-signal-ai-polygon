# bots/swing_pullback.py

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

MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_DISTANCE_FROM_EMA = float(os.getenv("PULLBACK_MAX_DIST_EMA", "1.0"))  # % from 20 EMA
MIN_PULLBACK_RVOL = float(os.getenv("PULLBACK_MIN_RVOL", "2.0"))

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
    Swing Pullback Bot — A+ dip in strong uptrend.

      • Strong uptrend:
          - 20 EMA > 50 EMA
          - Close above both EMAs.
      • Pullback:
          - At least 3 red days in last 5 (before today).
          - Peak-to-trough in last 5 days >= 5%.
      • Today:
          - Green candle.
          - Close within MAX_DISTANCE_FROM_EMA % of 20 EMA.
      • Filters:
          - Price >= MIN_PRICE
          - RVOL >= max(MIN_PULLBACK_RVOL, MIN_RVOL_GLOBAL)
          - Volume >= MIN_VOLUME_GLOBAL
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
        today_bar = days[-1]
        prev_bar = days[-2]

        last_price = closes[-1]
        prev_close = closes[-2]

        if last_price < MIN_PRICE:
            continue

        ema20 = _ema(closes[-40:], 20)
        ema50 = _ema(closes[-40:], 50)

        # Strong uptrend: 20 > 50 and price above both
        if not (ema20 > ema50 and last_price > ema20 and last_price > ema50):
            continue

        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(d.volume for d in recent) / len(recent)
        day_vol = float(today_bar.volume)
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

        # Today must be a green candle
        open_today = float(today_bar.open)
        if last_price <= open_today:
            continue

        dist_from_ema_pct = abs(last_price - ema20) / last_price * 100.0
        if dist_from_ema_pct > MAX_DISTANCE_FROM_EMA:
            continue

        # Last 5 days (before today) must show meaningful pullback
        recent_closes = closes[-6:-1]
        if len(recent_closes) < 5:
            continue

        red_days = sum(
            1 for i in range(1, len(recent_closes))
            if recent_closes[i] < recent_closes[i - 1]
        )

        high_5 = max(recent_closes)
        low_5 = min(recent_closes)
        drawdown_pct = (high_5 - low_5) / high_5 * 100.0 if high_5 > 0 else 0.0

        if red_days < 3:
            continue
        if drawdown_pct < 5.0:
            # pullback not deep enough to matter
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
            f"📉 Recent pullback: {red_days} red days, ~{drawdown_pct:.1f}% from high\n"
            f"📍 Close near 20 EMA (dist {dist_from_ema_pct:.1f}%)\n"
            f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("swing_pullback", sym, last_price, rvol, extra=extra)