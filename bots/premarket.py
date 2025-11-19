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
    send_alert,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

eastern = pytz.timezone("US/Eastern")

MIN_PREMARKET_MOVE_PCT = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "4.0"))
MIN_PREMARKET_PRICE = float(os.getenv("MIN_PREMARKET_PRICE", "2.0"))
MIN_PREMARKET_DOLLAR_VOL = float(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "2_000_000"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _is_premarket_bar(ts_ms: int) -> bool:
    """
    Return True if this minute bar is before 09:30 AM US/Eastern.
    Polygon timestamps are ms since epoch in UTC.
    """
    dt_utc = datetime.utcfromtimestamp(ts_ms / 1000.0).replace(tzinfo=pytz.utc)
    dt_et = dt_utc.astimezone(eastern)
    h = dt_et.hour
    m = dt_et.minute
    return (h < 9) or (h == 9 and m < 30)


async def run_premarket():
    """
    Premarket movers:
      • Move vs prior close >= MIN_PREMARKET_MOVE_PCT
      • Premarket price >= MIN_PREMARKET_PRICE
      • Premarket dollar volume >= MIN_PREMARKET_DOLLAR_VOL
    """
    if not POLYGON_KEY:
        print("[premarket] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[premarket] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # previous daily close
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=10)).isoformat(),
                    to=today_s,
                    limit=15,
                )
            )
        except Exception as e:
            print(f"[premarket] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 1:
            continue

        prev = days[-1]  # last completed day before today's session opens
        prev_close = float(prev.close)
        if prev_close <= 0:
            continue

        # intraday minute bars for today (premarket portion)
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
            print(f"[premarket] minute fetch failed for {sym}: {e}")
            continue

        pre_bars = [b for b in mins if _is_premarket_bar(int(b.timestamp))]
        if not pre_bars:
            continue

        open_pre = float(pre_bars[0].open)
        last_pre = float(pre_bars[-1].close)
        high_pre = max(float(b.high) for b in pre_bars)
        low_pre = min(float(b.low) for b in pre_bars)
        vol_pre = float(sum(b.volume for b in pre_bars))

        if last_pre < MIN_PREMARKET_PRICE:
            continue

        move_pre_pct = (last_pre - prev_close) / prev_close * 100.0
        if abs(move_pre_pct) < MIN_PREMARKET_MOVE_PCT:
            continue

        dollar_vol_pre = last_pre * vol_pre
        if dollar_vol_pre < MIN_PREMARKET_DOLLAR_VOL:
            continue

        direction_emoji = "🚀" if move_pre_pct > 0 else "📉"
        bias = "Long premarket momentum" if move_pre_pct > 0 else "Short / fade premarket move"

        extra = (
            f"{direction_emoji} Premarket move: {move_pre_pct:.1f}% vs prior close\n"
            f"📈 Prev Close: ${prev_close:.2f} → Premarket: ${last_pre:.2f}\n"
            f"📏 Premarket Range: ${low_pre:.2f} – ${high_pre:.2f}\n"
            f"📦 Premarket Volume: {int(vol_pre):,} (≈ ${dollar_vol_pre:,.0f})\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        # rvol not meaningful intraday pre-open → pass 0.0
        send_alert("premarket", sym, last_pre, 0.0, extra=extra)