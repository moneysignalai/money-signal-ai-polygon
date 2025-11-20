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
    now_est,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

MIN_RUN_PCT = float(os.getenv("MOM_RUN_MIN_PCT", "8.0"))
MIN_PULLBACK_PCT = float(os.getenv("MOM_PULLBACK_MIN_PCT", "3.0"))
MIN_MOM_PRICE = float(os.getenv("MOM_MIN_PRICE", "3.0"))
MIN_MOM_RVOL = float(os.getenv("MOM_MIN_RVOL", "2.0"))
MIN_MOM_DOLLAR_VOL = float(os.getenv("MOM_MIN_DOLLAR_VOL", "8000000"))


def _in_momentum_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    # only after 11:30 to catch late-day reversals
    return 11 * 60 + 30 <= mins <= 16 * 60


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


async def run_momentum_reversal():
    """
    Momentum Reversal Bot:

      • Stock runs ≥ MIN_RUN_PCT from open to high.
      • Then pulls back ≥ MIN_PULLBACK_PCT from high to close.
      • Filters on price, RVOL, volume, dollar volume.
      • Only scans after 11:30 AM ET (late-day swings).
    """
    if not POLYGON_KEY or not _client:
        print("[momentum_reversal] no API key/client; skipping.")
        return
    if not _in_momentum_window():
        print("[momentum_reversal] outside 11:30–16:00; skipping.")
        return

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # intraday 1d-agg to get OHLC and volume
        try:
            intrabars = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=today_s,
                    to=today_s,
                    limit=1,
                )
            )
        except Exception as e:
            print(f"[momentum_reversal] intraday fetch failed for {sym}: {e}")
            continue

        if not intrabars:
            continue

        ib = intrabars[0]
        day_open = float(ib.open)
        day_high = float(ib.high)
        day_low = float(ib.low)
        last_price = float(ib.close)
        vol_today = float(ib.volume)

        if last_price < MIN_MOM_PRICE:
            continue
        if day_open <= 0 or day_high <= 0:
            continue

        run_pct = (day_high - day_open) / day_open * 100.0
        if run_pct < MIN_RUN_PCT:
            continue

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
        if prev_close <= 0:
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

        if rvol < max(MIN_MOM_RVOL, MIN_RVOL_GLOBAL):
            continue

        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * vol_today
        if dollar_vol < MIN_MOM_DOLLAR_VOL:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0

        if last_price > day_open:
            bias = "Dip-buy opportunity after strong run"
        else:
            bias = "Mean-reversion short after exhaustion"

        from_high_pct = (day_high - last_price) / day_high * 100.0
        hod_text = f"{from_high_pct:.1f}% below HOD"

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        body = (
            f"🔄 Late-day momentum reversal\n"
            f"🚀 Run from open to high: {run_pct:.1f}%\n"
            f"📉 Pullback from high to close: {pullback_pct:.1f}%\n"
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f} · Close ${last_price:.2f} ({hod_text})\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 MOMENTUM_REVERSAL — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)