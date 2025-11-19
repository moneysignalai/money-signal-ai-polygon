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

# ---------------- CONFIG (with sane, looser defaults) ----------------

# "Monster" 1-minute bar threshold (shares)
# You can override via ENV, but you don't have to.
MIN_MONSTER_BAR_SHARES = int(os.getenv("MIN_MONSTER_BAR_SHARES", "2000000"))  # 2M by default

# Minimum price for the underlying
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))

# Per-bot RVOL floor (we still also respect MIN_RVOL_GLOBAL)
MIN_VOLUME_RVOL = float(os.getenv("MIN_VOLUME_RVOL", "2.0"))

# Run this bot at most once every X seconds (to avoid hammering Polygon)
_MONSTER_REFRESH_SEC = int(os.getenv("MONSTER_REFRESH_SEC", "480"))  # 8 minutes

_last_run_ts = 0.0

# Per-day, per-symbol de-dupe: one alert per ticker per day
_alert_date: date | None = None
_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted_syms


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _alerted_syms.add(sym)


def _in_rth_window() -> bool:
    """Regular trading hours 9:30–16:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    # Default: dynamic top-volume universe (~100 tickers, ~90% of market volume)
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_volume():
    """
    Volume Monster Bot (loosened):

      • Scan top-volume universe during RTH (9:30–16:00 EST).
      • Underlying close price >= MIN_MONSTER_PRICE.
      • Day RVOL >= max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL).
      • Day volume >= MIN_VOLUME_GLOBAL.
      • At least one 1-minute bar with volume >= MIN_MONSTER_BAR_SHARES
          (default 2,000,000 shares; was 8M before).
      • Throttled to once every MONSTER_REFRESH_SEC (default 480 sec).
      • Each symbol alerts at most once per day (per-day de-dupe).
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

    _reset_if_new_day()
    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            # we've already fired a monster-volume alert for this name today
            continue

        # ----- DAILY CONTEXT: price, RVOL, volume, move -----

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
        prev_bar = days[-2]

        prev_close = float(prev_bar.close)
        last_price = float(today_bar.close)

        if last_price < MIN_MONSTER_PRICE:
            continue

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        day_vol = float(today_bar.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        # Both global and bot-level RVOL floors
        if rvol < max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL):
            continue

        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # ----- INTRADAY MINUTE BARS: search for a monster bar -----

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

        monster_bar = None
        for b in mins:
            if b.volume >= MIN_MONSTER_BAR_SHARES:
                # keep the latest bar that qualifies
                monster_bar = b

        if not monster_bar:
            # no individual bar large enough
            continue

        monster_vol = float(monster_bar.volume)
        monster_price = float(monster_bar.close)

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0
            else 0.0
        )
        dollar_vol = last_price * day_vol
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

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
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)
        send_alert("volume", sym, last_price, rvol, extra=extra)