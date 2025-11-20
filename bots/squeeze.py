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
    now_est,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

MIN_SQUEEZE_PRICE = float(os.getenv("MIN_SQUEEZE_PRICE", "3.0"))
MIN_SQUEEZE_MOVE_PCT = float(os.getenv("MIN_SQUEEZE_MOVE_PCT", "9.0"))
MIN_SQUEEZE_RVOL = float(os.getenv("MIN_SQUEEZE_RVOL", "3.0"))
MIN_SQUEEZE_DOLLAR_VOL = float(os.getenv("MIN_SQUEEZE_DOLLAR_VOL", "15000000"))

# Optional: short-interest gate; if you wire in an external feed later
REQUIRE_SHORT_DATA = os.getenv("SQUEEZE_REQUIRE_SHORT_DATA", "false").lower() == "true"


def _in_squeeze_window() -> bool:
    now = now_est()
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


async def run_squeeze():
    """
    Short Squeeze Behaviour Bot:

      • Big % move, huge RVOL, big dollar volume.
      • Optional: when wired, will also require high short % + DTC.
    """
    if not POLYGON_KEY or not _client:
        print("[squeeze] no API key/client; skipping.")
        return
    if not _in_squeeze_window():
        print("[squeeze] outside RTH; skipping.")
        return

    universe = _get_universe()
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
        prev_bar = days[-2]

        prev_close = float(prev_bar.close)
        if prev_close <= 0:
            continue

        last_price = float(today_bar.close)
        day_high = float(today_bar.high)
        day_low = float(today_bar.low)

        if last_price < MIN_SQUEEZE_PRICE:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
        if abs(move_pct) < MIN_SQUEEZE_MOVE_PCT:
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

        day_vol = float(today_bar.volume)
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_SQUEEZE_DOLLAR_VOL:
            continue

        # Optional short-interest gate – left as placeholder
        si_text = "Short interest data not enforced"
        if REQUIRE_SHORT_DATA:
            # If you wire short-interest in later, you can enforce a gate here.
            # For now we just annotate the message.
            si_text = "Short interest gate enabled, but external data not wired."

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        hod_text = (
            "near high of day"
            if abs(day_high - last_price) / max(day_high, 1e-6) < 0.02
            else "off highs"
        )

        bias = (
            "Nuclear long squeeze candidate"
            if move_pct > 0
            else "Violent downside squeeze / liquidation"
        )

        body = (
            f"🔥 Short Squeeze Behaviour Detected\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f} · Close {hod_text}\n"
            f"📊 RVOL: {rvol:.1f}x · Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f})\n"
            f"📌 {si_text}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        # Nicely formatted timestamp like the other bots
        ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 SQUEEZE — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("squeeze", sym, last_price, rvol, extra=extra)