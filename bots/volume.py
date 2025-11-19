import os
from datetime import date, timedelta
from typing import List

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

MIN_VOLUME_RVOL = float(os.getenv("MIN_VOLUME_RVOL", "4.0"))
MIN_VOLUME_PRICE = float(os.getenv("MIN_VOLUME_PRICE", "2.0"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_volume():
    """
    High relative-volume scanner:
      • RVOL >= max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL)
      • Volume >= MIN_VOLUME_GLOBAL
      • Price >= MIN_VOLUME_PRICE
    """
    if not POLYGON_KEY:
        print("[volume] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[volume] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

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

        last_price = float(today_bar.close)
        if last_price < MIN_VOLUME_PRICE:
            continue

        prev_close = float(prev.close)
        move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0

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

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        dv = last_price * vol_today
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        if move_pct > 0:
            bias = "Long accumulation (high volume up day)"
        elif move_pct < 0:
            bias = "Heavy distribution (high volume down day)"
        else:
            bias = "High volume, flat price action"

        extra = (
            f"📊 High relative volume: {rvol:.1f}x\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Volume: {int(vol_today):,} (≈ ${dv:,.0f} notional)\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)