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
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

eastern = pytz.timezone("US/Eastern")

ORB_MINUTES = int(os.getenv("ORB_MINUTES", "15"))
ORB_MIN_RANGE_PCT = float(os.getenv("ORB_MIN_RANGE_PCT", "0.5"))   # min range as % of price
ORB_MIN_BREAK_FRAC = float(os.getenv("ORB_MIN_BREAK_FRAC", "0.25"))  # how far beyond edge vs range


def _in_orb_window() -> bool:
    """Only run 9:45–11:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 45 <= minutes <= 11 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_orb():
    """
    Opening Range Breakout (ORB) bot:
      • Uses first ORB_MINUTES to define range
      • Only triggers on strong, clean breakouts/breakdowns:
          - Range must be meaningful vs price (ORB_MIN_RANGE_PCT)
          - Last price must be at least ORB_MIN_BREAK_FRAC * range beyond edge
    """
    if not POLYGON_KEY:
        print("[orb] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[orb] Client not initialized; skipping scan.")
        return
    if not _in_orb_window():
        print("[orb] Outside 9:45–11:00 window; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Minute bars
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

        range_size = float(orb_high - orb_low)
        if range_size <= 0:
            continue

        mid_price = (orb_high + orb_low) / 2.0
        range_pct_vs_price = range_size / mid_price * 100.0 if mid_price > 0 else 0.0

        # Require a meaningful opening range vs price (avoid tiny/noise ranges)
        if range_pct_vs_price < ORB_MIN_RANGE_PCT:
            continue

        # Daily bars for RVOL / context
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

        if len(days) < 2:
            continue

        today_day = days[-1]
        prev_day = days[-2]
        prev_close = float(prev_day.close)

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

        # Clean breakout/breakdown requirement
        direction = None
        emoji = ""
        edge_price = None

        # Distance beyond edge
        if last_price > orb_high:
            dist = last_price - orb_high
            if dist >= ORB_MIN_BREAK_FRAC * range_size:
                direction = "Breakout above opening range"
                emoji = "🚀"
                edge_price = orb_high
        elif last_price < orb_low:
            dist = orb_low - last_price
            if dist >= ORB_MIN_BREAK_FRAC * range_size:
                direction = "Breakdown below opening range"
                emoji = "📉"
                edge_price = orb_low

        if not direction:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
        dv = last_price * vol_today
        grade = grade_equity_setup(abs(move_pct), rvol, dv)
        bias = "Long ORB breakout" if last_price > orb_high else "Short ORB breakdown"

        # Simple trade idea: range-based R multiple
        if edge_price is not None:
            if last_price > orb_high:
                entry = edge_price
                risk = orb_low
                risk_per_share = max(entry - risk, 0.01)
                target = entry + 1.5 * risk_per_share
                idea = f"Long > {entry:.2f}, risk {risk:.2f}, target ~{target:.2f}"
            else:
                entry = edge_price
                risk = orb_high
                risk_per_share = max(risk - entry, 0.01)
                target = entry - 1.5 * risk_per_share
                idea = f"Short < {entry:.2f}, risk {risk:.2f}, target ~{target:.2f}"
        else:
            idea = "Use range high/low as trigger & risk."

        extra = (
            f"{emoji} {direction} ({ORB_MINUTES}-min)\n"
            f"📏 Range: {orb_low:.2f} – {orb_high:.2f} (size {range_size:.2f}, {range_pct_vs_price:.2f}% of price)\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Vol: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🧠 Idea: {idea}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("orb", sym, last_price, rvol, extra=extra)