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

MIN_GAP_PRICE = float(os.getenv("MIN_GAP_PRICE", "2.0"))
MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "4.0"))      # min |gap|
MAX_GAP_PCT = float(os.getenv("MAX_GAP_PCT", "35.0"))     # cap crazy microcaps
MIN_GAP_RVOL = float(os.getenv("MIN_GAP_RVOL", "2.0"))
MIN_GAP_DOLLAR_VOL = float(os.getenv("MIN_GAP_DOLLAR_VOL", "5000000"))  # $5M+

# Only 9:30–10:30
def _in_gap_window() -> bool:
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 10 * 60 + 30


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


async def run_gap():
    """
    Overnight Gap Radar (up or down):

      • |Gap| between MIN_GAP_PCT and MAX_GAP_PCT vs prior close
      • Both gap-up and gap-down permitted
      • Last price >= MIN_GAP_PRICE
      • RVOL >= max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL)
      • Volume >= MIN_VOLUME_GLOBAL
      • Only during 9:30–10:30 AM EST
      • Only runs once per calendar day per container
    """
    if not POLYGON_KEY or not _client:
        print("[gap] no API key/client; skipping.")
        return
    if not _in_gap_window():
        print("[gap] outside 9:30–10:30; skipping.")
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
        day_high = float(today_bar.high)
        day_low = float(today_bar.low)
        vol_today = float(today_bar.volume)

        if last_price < MIN_GAP_PRICE:
            continue

        # basic gap stats
        gap_pct = (open_today - prev_close) / prev_close * 100.0
        if abs(gap_pct) < MIN_GAP_PCT or abs(gap_pct) > MAX_GAP_PCT:
            continue

        intraday_pct = (
            (last_price - open_today) / open_today * 100.0
            if open_today > 0
            else 0.0
        )
        total_move_pct = (last_price - prev_close) / prev_close * 100.0

        # RVOL
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = vol_today

        if avg_vol > 0:
            rvol = vol_today / avg_vol
        else:
            rvol = 1.0

        if rvol < max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL):
            continue
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * vol_today
        if dollar_vol < MIN_GAP_DOLLAR_VOL:
            continue

        grade = grade_equity_setup(abs(total_move_pct), rvol, dollar_vol)

        if gap_pct > 0:
            emoji = "🚀"
            direction = "Gap-up"
            if intraday_pct > 0:
                bias = "Gap-and-go long setup"
            else:
                bias = "Gap-up being faded (possible short)"
        else:
            emoji = "⚠️"
            direction = "Gap-down"
            if intraday_pct < 0:
                bias = "Gap-down continuation short"
            else:
                bias = (
                    "Gap-down being bought (possible reversal)"
                    if total_move_pct < 0
                    else "Gap-down being bought (possible reversal)"
                )

        body = (
            f"{emoji} {direction}: {gap_pct:.1f}% vs prior close\n"
            f"📈 Prev Close: ${prev_close:.2f} → Open: ${open_today:.2f} → Last: ${last_price:.2f}\n"
            f"📊 Intraday from open: {intraday_pct:.1f}% · Total move: {total_move_pct:.1f}%\n"
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f}\n"
            f"📦 Day Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 GAP — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("gap", sym, last_price, rvol, extra=extra)