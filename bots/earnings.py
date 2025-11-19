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
    is_etf_blacklisted,
    grade_equity_setup,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

EARNINGS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_LOOKBACK_DAYS", "2"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "5.0"))
MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "2.0"))


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
    Earnings-driven movers with context:
      • move vs yesterday
      • gap vs intraday
      • RVOL + volume
      • setup grade + bias
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
        if is_etf_blacklisted(sym):
            continue

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

        last_price = float(today_bar.close)
        if last_price < MIN_EARNINGS_PRICE:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
        if abs(move_pct) < MIN_EARNINGS_MOVE_PCT:
            continue

        today_open = float(today_bar.open)
        if today_open > 0:
            gap_pct = (today_open - prev_close) / prev_close * 100.0
            intraday_pct = (last_price - today_open) / today_open * 100.0
        else:
            gap_pct = 0.0
            intraday_pct = move_pct

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

        dv = last_price * vol_today
        grade = grade_equity_setup(abs(move_pct), rvol, dv)
        bias = "Long earnings momentum" if move_pct > 0 else "Short / fade earnings move"

        extra = (
            f"📣 Earnings move: {move_pct:.1f}% today\n"
            f"📈 Prev Close: ${prev_close:.2f} → Open: ${today_open:.2f} → Close: ${last_price:.2f}\n"
            f"📊 Gap: {gap_pct:.1f}% · Intraday: {intraday_pct:.1f}% from open\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("earnings", sym, last_price, rvol, extra=extra)