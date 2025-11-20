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

# ------------------- CONFIG -------------------

MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_PRICE = float(os.getenv("PULLBACK_MAX_PRICE", "200.0"))
MIN_DOLLAR_VOL = float(os.getenv("PULLBACK_MIN_DOLLAR_VOL", "30000000"))  # $30M+
MIN_RVOL = float(os.getenv("PULLBACK_MIN_RVOL", "2.0"))
MAX_PULLBACK_PCT = float(os.getenv("PULLBACK_MAX_PULLBACK_PCT", "15.0"))
MIN_PULLBACK_PCT = float(os.getenv("PULLBACK_MIN_PULLBACK_PCT", "3.0"))
MAX_RED_DAYS = int(os.getenv("PULLBACK_MAX_RED_DAYS", "3"))
LOOKBACK_DAYS = int(os.getenv("PULLBACK_LOOKBACK_DAYS", "60"))

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


# ------------------- HELPERS -------------------


def _sma(values: List[float], window: int) -> List[float]:
    if len(values) < window:
        return []
    out = []
    for i in range(window - 1, len(values)):
        window_vals = values[i - window + 1 : i + 1]
        out.append(sum(window_vals) / float(window))
    return out


async def run_swing_pullback():
    """
    Swing Pullback Bot — strong uptrend, clean dip into support.

      • Time: RTH only (09:30–16:00 ET)
      • Universe: TICKER_UNIVERSE env OR dynamic top volume universe (200 names)
      • Filters (per symbol):
          - Price between MIN_PRICE and MAX_PRICE
          - Day $ volume ≥ MIN_DOLLAR_VOL
          - RVOL ≥ max(MIN_RVOL_GLOBAL, MIN_RVOL)
          - Clear uptrend: 20-period SMA > 50-period SMA
          - Recent 1–MAX_RED_DAYS red candles
          - Pullback from recent swing high between MIN_PULLBACK_PCT and MAX_PULLBACK_PCT
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

        # --- Fetch daily data ---
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=LOOKBACK_DAYS + 30)).isoformat(),
                    to=today_s,
                    limit=LOOKBACK_DAYS + 30,
                )
            )
        except Exception as e:
            print(f"[swing_pullback] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 30:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        try:
            last_price = float(today_bar.close)
            prev_close = float(prev_bar.close)
            day_vol = float(today_bar.volume or 0.0)
        except Exception:
            continue

        if last_price < MIN_PRICE or last_price > MAX_PRICE:
            continue

        dollar_vol = last_price * day_vol
        # FIX: use built-in max(), not undefined MAX
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

        # --- Trend: 20SMA vs 50SMA ---
        closes = [float(d.close) for d in days]
        sma20_series = _sma(closes, 20)
        sma50_series = _sma(closes, 50)
        if not sma20_series or not sma50_series:
            continue

        sma20 = sma20_series[-1]
        sma50 = sma50_series[-1]

        # Strong uptrend
        if sma20 <= sma50:
            continue
        if last_price <= sma50:
            continue

        # --- Recent pullback ---
        recent_closes = [float(d.close) for d in days[-(MAX_RED_DAYS + 5) :]]
        red_days = 0
        for i in range(1, len(recent_closes)):
            if recent_closes[i] < recent_closes[i - 1]:
                red_days += 1

        if red_days == 0 or red_days > MAX_RED_DAYS:
            continue

        # Pullback from recent swing high
        recent_window = closes[-20:]
        swing_high = max(recent_window)
        if swing_high <= 0:
            continue

        pullback_pct = (swing_high - last_price) / swing_high * 100.0
        if pullback_pct < MIN_PULLBACK_PCT or pullback_pct > MAX_PULLBACK_PCT:
            continue

        # Day move vs yesterday
        move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0

        grade = grade_equity_setup(move_pct, rvol, dollar_vol)

        # --- Alert formatting (A-style) ---
        timestamp = datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")
        emoji = "📈"
        money_emoji = "💰"
        divider = "────────────"

        extra = (
            f"{emoji} SWING PULLBACK — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"📌 Strong uptrend: 20 SMA {sma20:.2f} > 50 SMA {sma50:.2f}\n"
            f"📉 Recent pullback: {red_days} red days, ~{pullback_pct:.1f}% from high\n"
            f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("swing_pullback", sym, last_price, rvol, extra=extra)