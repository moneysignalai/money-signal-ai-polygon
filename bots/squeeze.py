import os
from datetime import date, timedelta
from typing import List

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_VOLUME_GLOBAL,
    MIN_RVOL_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_SQUEEZE_PCT = float(os.getenv("MIN_SQUEEZE_PCT", "12.0"))
MIN_SQUEEZE_RVOL = float(os.getenv("MIN_SQUEEZE_RVOL", "4.0"))
MIN_SQUEEZE_PRICE = float(os.getenv("MIN_SQUEEZE_PRICE", "2.0"))


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_squeeze():
    """
    Short-squeeze style scanner.

    Requirements:
      • Move >= MIN_SQUEEZE_PCT since yesterday's close
      • Last price >= MIN_SQUEEZE_PRICE
      • RVOL >= max(MIN_SQUEEZE_RVOL, MIN_RVOL_GLOBAL)
      • Volume >= MIN_VOLUME_GLOBAL
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
        if is_etf_blacklisted(sym):
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

        if rvol < max(MIN_SQUEEZE_RVOL, MIN_RVOL_GLOBAL):
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        high = float(today_bar.high)
        dv = last_price * vol_today
        grade = grade_equity_setup(change_pct, rvol, dv)

        move_emoji = "🚀" if change_pct > 0 else "📉"
        bias = "Long momentum" if change_pct > 0 else "Short momentum"

        # Position vs HOD
        if high > 0:
            from_high_pct = (high - last_price) / high * 100.0
        else:
            from_high_pct = 0.0

        if abs(from_high_pct) < 1.0:
            hod_text = "at/near HOD"
        else:
            hod_text = f"{from_high_pct:.1f}% below HOD"

        extra = (
            f"{move_emoji} Short-squeeze style move: {change_pct:.1f}% today\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} (High: ${high:.2f})\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"📍 Position vs High: {hod_text}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("squeeze", sym, last_price, rvol, extra=extra)