import os
from datetime import date, timedelta
from typing import List, Tuple, Optional

try:
    from massive import RESTClient  # Massive (formerly Polygon.io)
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _calc_rvol_and_volume(sym: str) -> Optional[Tuple[float, float, float]]:
    """
    Returns (last_price, volume_today, rvol) or None on failure.
    Uses minute bars for intraday volume and daily bars for 20-day average.
    """
    if not _client:
        return None

    today = date.today()
    from_str = today.isoformat()
    to_str = today.isoformat()

    try:
        mins = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="minute",
                from_=from_str,
                to=to_str,
                limit=10_000,
            )
        )
        if not mins:
            return None

        vol_today = float(sum(b.volume for b in mins))
        last_price = float(mins[-1].close)

        hist = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=(today - timedelta(days=30)).isoformat(),
                to=(today - timedelta(days=1)).isoformat(),
                limit=50,
            )
        )
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = vol_today

        if avg_vol <= 0:
            rvol = 1.0
        else:
            rvol = vol_today / avg_vol

        return last_price, vol_today, rvol
    except Exception as e:
        print(f"[volume] Failed to fetch data for {sym}: {e}")
        return None


async def run_volume():
    """
    Top 25 daily volume scanner (also acts like a premarket/high-volume scanner).
    """
    if not POLYGON_KEY:
        print("[volume] POLYGON_KEY not set; skipping scan.")
        return

    universe = _get_ticker_universe()
    results: List[Tuple[str, float, float, float]] = []

    for sym in universe:
        data = _calc_rvol_and_volume(sym)
        if not data:
            continue
        last_price, vol_today, rvol = data
        results.append((sym, last_price, vol_today, rvol))

    if not results:
        print("[volume] No data for any symbols in universe.")
        return

    results.sort(key=lambda x: x[2], reverse=True)
    top = results[:25]

    for sym, last_price, vol_today, rvol in top:
        if vol_today < MIN_VOLUME_GLOBAL:
            continue
        if rvol < MIN_RVOL_GLOBAL:
            continue

        extra = f"Top volume name today.\nVolume {int(vol_today):,} · RVOL {rvol:.1f}x"
        send_alert("volume", sym, last_price, rvol, extra=extra)