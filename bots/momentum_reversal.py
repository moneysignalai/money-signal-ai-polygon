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

MIN_RUN_PCT = float(os.getenv("MIN_REVERSAL_RUN_PCT", "8.0"))       # move up from open to HOD
MIN_PULLBACK_PCT = float(os.getenv("MIN_REVERSAL_PULLBACK_PCT", "3.0"))  # from HOD down to close
MIN_REVERSAL_PRICE = float(os.getenv("MIN_REVERSAL_PRICE", "2.0"))
MIN_REVERSAL_RVOL = float(os.getenv("MIN_REVERSAL_RVOL", "2.5"))


def _in_reversal_window() -> bool:
    """Only run 9:30–16:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_momentum_reversal():
    """
    Momentum Reversal:
      • Stock runs ≥ MIN_RUN_PCT from open to HOD
      • Then pulls back ≥ MIN_PULLBACK_PCT from HOD to close
      • Price >= MIN_REVERSAL_PRICE
      • RVOL + volume filters
      • Use as dip-buy / short-entry context
    """
    if not POLYGON_KEY:
        print("[momentum_reversal] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[momentum_reversal] Client not initialized; skipping scan.")
        return
    if not _in_reversal_window():
        print("[momentum_reversal] Outside 9:30–16:00 window; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # intraday minute bars
        try:
            mins = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="minute",
                    from_=today_s,
                    to=today_s,
                    limit=10_000,
                )
            )
        except Exception as e:
            print(f"[momentum_reversal] minute fetch failed for {sym}: {e}")
            continue

        if len(mins) < 20:
            continue

        open_price = float(mins[0].open)
        last_price = float(mins[-1].close)
        day_high = max(float(b.high) for b in mins)
        day_low = min(float(b.low) for b in mins)

        if open_price <= 0 or last_price < MIN_REVERSAL_PRICE:
            continue

        # Run up from open to high
        run_pct = (day_high - open_price) / open_price * 100.0
        if run_pct < MIN_RUN_PCT:
            continue

        # Pullback from high to close
        pullback_pct = (day_high - last_price) / day_high * 100.0
        if pullback_pct < MIN_PULLBACK_PCT:
            continue

        # Daily bars for RVOL / volume / prev close
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[momentum_reversal] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev = days[-2]
        prev_close = float(prev.close)

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        if avg_vol > 0:
            rvol = float(today_bar.volume) / avg_vol
        else:
            rvol = 1.0

        if rvol < max(MIN_REVERSAL_RVOL, MIN_RVOL_GLOBAL):
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
        dv = last_price * vol_today
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        # Bias explanation: could be dip-buy or short
        bias = (
            "Potential dip-buy zone after strong run"
            if move_pct > 0
            else "Potential short entry after failed run"
        )

        extra = (
            f"🔄 Momentum reversal after strong run\n"
            f"🚀 Run from open to high: {run_pct:.1f}%\n"
            f"📉 Pullback from high to close: {pullback_pct:.1f}%\n"
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f} · Close ${last_price:.2f}\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)