import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_SQUEEZE_PCT = float(os.getenv("MIN_SQUEEZE_PCT", "10.0"))  # 10%+ move
MIN_SQUEEZE_RVOL = float(os.getenv("MIN_SQUEEZE_RVOL", "3.0"))  # 3x RVOL by default


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_squeeze():
    """
    Short-squeeze style scanner.
    """
    if not POLYGON_KEY:
        print("[squeeze] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[squeeze] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
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
            print(f"[squeeze] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev = days[-2]

        prev_close = float(prev.close)
        if prev_close <= 0:
            continue

        change_pct = (float(today_bar.close) - prev_close) / prev_close * 100.0
        if change_pct < MIN_SQUEEZE_PCT:
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

        if rvol < MIN_SQUEEZE_RVOL:
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        last_price = float(today_bar.close)
        high = float(today_bar.high)

        extra = (
            f"Short-squeeze style move {change_pct:.1f}% today\n"
            f"Close ${last_price:.2f} (High ${high:.2f}) from Prev Close ${prev_close:.2f}\n"
            f"RVOL {rvol:.1f}x · Volume {int(vol_today):,}"
        )

        send_alert("squeeze", sym, last_price, rvol, extra=extra)