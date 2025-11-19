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
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "4.0"))        # min % gap vs prev close
MIN_GAP_PRICE = float(os.getenv("MIN_GAP_PRICE", "2.0"))    # min price
MIN_GAP_RVOL = float(os.getenv("MIN_GAP_RVOL", "2.0"))      # min RVOL for valid gap


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_gap():
    """
    Gap scanner:
      • Open vs prior close >= MIN_GAP_PCT
      • Price >= MIN_GAP_PRICE
      • RVOL >= max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL)
      • Volume >= MIN_VOLUME_GLOBAL
    """
    if not POLYGON_KEY:
        print("[gap] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[gap] Client not initialized; skipping scan.")
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
            print(f"[gap] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev = days[-2]

        prev_close = float(prev.close)
        if prev_close <= 0:
            continue

        open_today = float(today_bar.open)
        last_price = float(today_bar.close)
        if last_price < MIN_GAP_PRICE:
            continue

        gap_pct = (open_today - prev_close) / prev_close * 100.0
        if abs(gap_pct) < MIN_GAP_PCT:
            continue

        # RVOL
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

        if rvol < max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL):
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        total_move_pct = (last_price - prev_close) / prev_close * 100.0
        if open_today > 0:
            intraday_pct = (last_price - open_today) / open_today * 100.0
        else:
            intraday_pct = 0.0

        dv = last_price * vol_today
        grade = grade_equity_setup(abs(total_move_pct), rvol, dv)
        if total_move_pct > 0:
            bias = "Long gap-and-go"
        else:
            bias = "Short / fade gap"

        direction_emoji = "🚀" if gap_pct > 0 else "📉"

        extra = (
            f"{direction_emoji} Gap vs prior close: {gap_pct:.1f}%\n"
            f"📈 Prev Close: ${prev_close:.2f} → Open: ${open_today:.2f} → Close: ${last_price:.2f}\n"
            f"📊 Intraday from open: {intraday_pct:.1f}% · Total move: {total_move_pct:.1f}%\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("gap", sym, last_price, rvol, extra=extra)