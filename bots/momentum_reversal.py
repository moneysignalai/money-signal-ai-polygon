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

# % drop from open to low before reversal
MIN_REVERSAL_DROP_PCT = float(os.getenv("MIN_REVERSAL_DROP_PCT", "4.0"))
# % bounce from low to close
MIN_REVERSAL_BOUNCE_PCT = float(os.getenv("MIN_REVERSAL_BOUNCE_PCT", "4.0"))
# min price
MIN_REVERSAL_PRICE = float(os.getenv("MIN_REVERSAL_PRICE", "2.0"))
# min RVOL
MIN_REVERSAL_RVOL = float(os.getenv("MIN_REVERSAL_RVOL", "2.5"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_momentum_reversal():
    """
    Bullish intraday reversal scanner:
      • Price sells off from open to low >= MIN_REVERSAL_DROP_PCT
      • Then bounces from low to close >= MIN_REVERSAL_BOUNCE_PCT
      • Close > open
      • Price >= MIN_REVERSAL_PRICE
      • RVOL and volume filters
    """
    if not POLYGON_KEY:
        print("[momentum_reversal] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[momentum_reversal] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Minute bars for intraday pattern
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
        day_low = min(float(b.low) for b in mins)
        day_high = max(float(b.high) for b in mins)

        if open_price <= 0 or last_price < MIN_REVERSAL_PRICE:
            continue

        drop_pct = (day_low - open_price) / open_price * 100.0  # negative
        bounce_pct = (last_price - day_low) / day_low * 100.0

        # We want a real selloff first (drop <= -MIN_REVERSAL_DROP_PCT)
        if drop_pct > -MIN_REVERSAL_DROP_PCT:
            continue

        # And a meaningful bounce
        if bounce_pct < MIN_REVERSAL_BOUNCE_PCT:
            continue

        # Close should be above open (bullish reversal)
        if last_price <= open_price:
            continue

        # Daily bars for RVOL / volume
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
        bias = "Long reversal from intraday selloff"

        if day_high > 0:
            from_high_pct = (day_high - last_price) / day_high * 100.0
        else:
            from_high_pct = 0.0

        if abs(from_high_pct) < 1.0:
            hod_text = "at/near HOD"
        else:
            hod_text = f"{from_high_pct:.1f}% below HOD"

        extra = (
            f"🔄 Bullish intraday reversal detected\n"
            f"📉 Drop from open to low: {drop_pct:.1f}%\n"
            f"📈 Bounce from low to close: {bounce_pct:.1f}%\n"
            f"📏 Range: Low ${day_low:.2f} – High ${day_high:.2f} · Close ${last_price:.2f}\n"
            f"📍 Position vs High: {hod_text}\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)