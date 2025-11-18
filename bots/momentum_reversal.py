import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_RVOL_GLOBAL, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_INTRADAY_RANGE_PCT = float(os.getenv("MOMO_MIN_RANGE_PCT", "3.0"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_momentum_reversal():
    """
    Momentum reversal bot.
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

        if len(mins) < 30:
            continue

        first_bar = mins[0]
        last_bar = mins[-1]
        day_open = float(first_bar.open)
        last_price = float(last_bar.close)
        intraday_high = max(b.high for b in mins)
        intraday_low = min(b.low for b in mins)

        if day_open <= 0:
            continue

        range_pct = (intraday_high - intraday_low) / day_open * 100.0
        if range_pct < MIN_INTRADAY_RANGE_PCT:
            continue

        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=30)).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[momentum_reversal] daily fetch failed for {sym}: {e}")
            continue

        if not days:
            continue

        today_day = days[-1]
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_day.volume)

        if avg_vol > 0:
            rvol = float(today_day.volume) / avg_vol
        else:
            rvol = 1.0

        if rvol < MIN_RVOL_GLOBAL:
            continue

        vol_today = float(today_day.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        direction = None
        if intraday_high > day_open * (1 + MIN_INTRADAY_RANGE_PCT / 100.0) and last_price < day_open:
            direction = "Bearish Reversal (failed breakout)"
        elif intraday_low < day_open * (1 - MIN_INTRADAY_RANGE_PCT / 100.0) and last_price > day_open:
            direction = "Bullish Reversal (failed breakdown)"

        if not direction:
            continue

        extra = (
            f"{direction}\n"
            f"Open {day_open:.2f} · High {intraday_high:.2f} · Low {intraday_low:.2f} · Last {last_price:.2f}\n"
            f"Range {range_pct:.1f}% · RVOL {rvol:.1f}x · Volume {int(vol_today):,}"
        )
        send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)