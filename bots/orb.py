import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_RVOL_GLOBAL, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

ORB_MINUTES = int(os.getenv("ORB_MINUTES", "15"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_orb():
    """
    Opening Range Breakout (ORB) bot.
    """
    if not POLYGON_KEY:
        print("[orb] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[orb] Client not initialized; skipping scan.")
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
            print(f"[orb] minute fetch failed for {sym}: {e}")
            continue

        if len(mins) < ORB_MINUTES + 5:
            continue

        orb_slice = mins[:ORB_MINUTES]
        rest = mins[ORB_MINUTES:]
        if not rest:
            continue

        orb_high = max(b.high for b in orb_slice)
        orb_low = min(b.low for b in orb_slice)
        last_bar = rest[-1]
        last_price = float(last_bar.close)

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
            print(f"[orb] daily fetch failed for {sym}: {e}")
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
        emoji = ""
        if last_price > orb_high:
            direction = "Breakout above opening range"
            emoji = "🚀"
        elif last_price < orb_low:
            direction = "Breakdown below opening range"
            emoji = "📉"

        if not direction:
            continue

        extra = (
            f"{emoji} {direction} ({ORB_MINUTES}-min)\n"
            f"📏 Range: {orb_low:.2f} – {orb_high:.2f}\n"
            f"📦 Day Vol: {int(vol_today):,}"
        )

        send_alert("orb", sym, last_price, rvol, extra=extra)