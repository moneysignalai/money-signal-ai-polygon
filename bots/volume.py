import os
import time
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

MIN_MONSTER_BAR_SHARES = int(os.getenv("MIN_MONSTER_BAR_SHARES", "8000000"))
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))
MIN_VOLUME_RVOL = float(os.getenv("MIN_VOLUME_RVOL", "2.5"))

# run this bot at most every 8 minutes
_MONSTER_REFRESH_SEC = int(os.getenv("MONSTER_REFRESH_SEC", "480"))
_last_run_ts = 0.0


def _in_rth_window() -> bool:
    """Regular trading hours 9:30–16:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_volume():
    """
    Volume Monster:
      • Any 1-min bar with volume >= MIN_MONSTER_BAR_SHARES
      • Underlying price >= MIN_MONSTER_PRICE
      • Day RVOL >= max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL)
      • Day volume >= MIN_VOLUME_GLOBAL
      • Only during 9:30–16:00 EST
      • Bot-level throttle: at most once every MONSTER_REFRESH_SEC
    """
    global _last_run_ts

    if not POLYGON_KEY:
        print("[volume] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[volume] Client not initialized; skipping scan.")
        return
    if not _in_rth_window():
        print("[volume] Outside 9:30–16:00 window; skipping scan.")
        return

    now_ts = time.time()
    if now_ts - _last_run_ts < _MONSTER_REFRESH_SEC:
        print("[volume] Throttled by MONSTER_REFRESH_SEC; skipping this cycle.")
        return
    _last_run_ts = now_ts

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # daily bars for RVOL / move
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
            print(f"[volume] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev = days[-2]
        prev_close = float(prev.close)
        last_price = float(today_bar.close)

        if last_price < MIN_MONSTER_PRICE:
            continue

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

        if rvol < max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL):
            continue

        day_vol = float(today_bar.volume)
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # minute bars for monster bar detection
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
            print(f"[volume] minute fetch failed for {sym}: {e}")
            continue

        if not mins:
            continue

        # look for any 1-min bar with massive shares
        monster_bar = None
        for b in mins:
            if b.volume >= MIN_MONSTER_BAR_SHARES:
                monster_bar = b
        # if multiple qualify, we'll just use the last one

        if not monster_bar:
            continue

        monster_vol = float(monster_bar.volume)
        monster_price = float(monster_bar.close)

        move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
        dv = last_price * day_vol
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        if move_pct > 0:
            bias = "Aggressive buying (monster 1-min volume)"
        elif move_pct < 0:
            bias = "Aggressive selling (monster 1-min volume)"
        else:
            bias = "Huge tape activity with flat price"

        extra = (
            f"📊 Monster 1-min bar: {int(monster_vol):,} shares\n"
            f"💹 Bar Close: ${monster_price:.2f}\n"
            f"📈 Prev Close: ${prev_close:.2f} → Day Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dv:,.0f} notional)\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)