import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_RVOL_GLOBAL, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

EARNINGS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "2"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "3.0"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("EARNINGS_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _had_recent_earnings_news(sym: str, client: RESTClient, today: date) -> bool:
    start = (today - timedelta(days=EARNINGS_LOOKBACK_DAYS)).isoformat()
    try:
        news_iter = client.list_ticker_news(
            ticker=sym,
            limit=20,
        )
    except Exception as e:
        print(f"[earnings] list_ticker_news failed for {sym}: {e}")
        return False

    for n in news_iter:
        published = getattr(n, "published_utc", None)
        if not published:
            continue
        if isinstance(published, str) and published[:10] < start:
            continue

        text = (getattr(n, "title", "") + " " + getattr(n, "description", "")).lower()
        if "earnings" in text or "results" in text:
            return True

    return False


async def run_earnings():
    """
    Earnings-driven movers.
    """
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[earnings] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if not _had_recent_earnings_news(sym, _client, today):
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
            print(f"[earnings] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev = days[-2]

        prev_close = float(prev.close)
        if prev_close <= 0:
            continue

        move_pct = (float(today_bar.close) - prev_close) / prev_close * 100.0
        if abs(move_pct) < MIN_EARNINGS_MOVE_PCT:
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

        if rvol < MIN_RVOL_GLOBAL:
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        last_price = float(today_bar.close)

        extra = (
            f"Earnings move {move_pct:.1f}% today\n"
            f"Prev Close ${prev_close:.2f} → Close ${last_price:.2f}\n"
            f"RVOL {rvol:.1f}x · Volume {int(vol_today):,}"
        )

        send_alert("earnings", sym, last_price, rvol, extra=extra)