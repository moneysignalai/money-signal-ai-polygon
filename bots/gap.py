import os
from datetime import date, timedelta
from typing import List, Optional, Tuple

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, MIN_RVOL_GLOBAL, MIN_VOLUME_GLOBAL, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "5.0"))  # 5% default


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _calc_intraday_volume_and_price(sym: str, today: date) -> Optional[Tuple[float, float]]:
    if not _client:
        return None
    s = today.isoformat()
    try:
        mins = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="minute",
                from_=s,
                to=s,
                limit=10_000,
            )
        )
        if not mins:
            return None
        vol_today = float(sum(b.volume for b in mins))
        last_price = float(mins[-1].close)
        return last_price, vol_today
    except Exception as e:
        print(f"[gap] intraday fetch failed for {sym}: {e}")
        return None


async def run_gap():
    """
    Gap up / gap down scanner.
    """
    if not POLYGON_KEY:
        print("[gap] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[gap] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()

    for sym in universe:
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=10)).isoformat(),
                    to=today.isoformat(),
                    limit=15,
                )
            )
        except Exception as e:
            print(f"[gap] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        prev = days[-2]
        today_bar = days[-1]

        prev_close = float(prev.close)
        today_open = float(today_bar.open or today_bar.close)

        if prev_close <= 0:
            continue

        gap_pct = (today_open - prev_close) / prev_close * 100.0
        if abs(gap_pct) < MIN_GAP_PCT:
            continue

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        if avg_vol <= 0:
            rvol = 1.0
        else:
            rvol = float(today_bar.volume) / avg_vol

        if rvol < MIN_RVOL_GLOBAL:
            continue

        intraday = _calc_intraday_volume_and_price(sym, today)
        if not intraday:
            continue
        last_price, vol_today = intraday

        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        direction = "GAP UP" if gap_pct > 0 else "GAP DOWN"
        extra = (
            f"{direction} {gap_pct:.1f}%\n"
            f"Prev Close ${prev_close:.2f} → Open ${today_open:.2f}\n"
            f"RVOL {rvol:.1f}x · Intraday Vol {int(vol_today):,}"
        )

        send_alert("gap", sym, last_price, rvol, extra=extra)