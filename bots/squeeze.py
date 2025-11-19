import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_SQUEEZE_PCT = float(os.getenv("MIN_SQUEEZE_PCT", "12.0"))    # 12%+ move
MIN_SQUEEZE_RVOL = float(os.getenv("MIN_SQUEEZE_RVOL", "4.0"))   # 4x RVOL
MIN_SQUEEZE_PRICE = float(os.getenv("MIN_SQUEEZE_PRICE", "2.0")) # filter out sub-$2 junk


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

        last_price = float(today_bar.close)
        if last_price < MIN_SQUEEZE_PRICE:
            # cut out 11-cent penny stock type names
            continue

        change_pct = (last_price - prev_close) / prev_close * 100.0
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

        high = float(today_bar.high)
        move_emoji = "🚀" if change_pct > 0 else "📉"

        extra = (
            f"{move_emoji} Short-squeeze style move: {change_pct:.1f}% today\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} (High: ${high:.2f})\n"
            f"📦 Volume: {int(vol_today):,}"
        )

        send_alert("squeeze", sym, last_price, rvol, extra=extra)