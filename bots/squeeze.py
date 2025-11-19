import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple

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

# Squeeze thresholds
MIN_SQUEEZE_PRICE = float(os.getenv("MIN_SQUEEZE_PRICE", "3.0"))
MIN_SQUEEZE_MOVE_PCT = float(os.getenv("MIN_SQUEEZE_MOVE_PCT", "9.0"))   # % gain vs prior close
MIN_SQUEEZE_RVOL = float(os.getenv("MIN_SQUEEZE_RVOL", "4.0"))           # RVOL explosion
MIN_SQUEEZE_DOLLAR_VOL = float(os.getenv("MIN_SQUEEZE_DOLLAR_VOL", "20000000"))  # $20M

# Optional short-interest filter
USE_SHORT_INTEREST_FILTER = os.getenv("USE_SHORT_INTEREST_FILTER", "false").lower() == "true"
MIN_SHORT_PERCENT = float(os.getenv("MIN_SHORT_PERCENT", "22.0"))
MIN_DTC = float(os.getenv("MIN_DTC", "5.5"))  # days-to-cover


def _in_squeeze_window() -> bool:
    """Short Squeeze Pro: RTH 9:30–16:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def get_short_interest(sym: str) -> Optional[Tuple[float, float]]:
    """
    Hook for short-interest data.

    Return:
        (short_percent_of_float, days_to_cover)

    For now this is a stub that returns None.
    Later you can wire this into a DB or API that you populate
    from FINRA/Nasdaq/Ortex/etc and flip USE_SHORT_INTEREST_FILTER=true.
    """
    # Example of what a real implementation might look like:
    # rec = your_short_db.get(sym)
    # if not rec: return None
    # return (rec.short_percent, rec.days_to_cover)
    return None


async def run_squeeze():
    """
    Short Squeeze Pro (behavioral version):

      • Price >= MIN_SQUEEZE_PRICE
      • Gain vs prior close >= MIN_SQUEEZE_MOVE_PCT
      • Day RVOL >= max(MIN_SQUEEZE_RVOL, MIN_RVOL_GLOBAL)
      • Day volume >= MIN_VOLUME_GLOBAL
      • Dollar volume >= MIN_SQUEEZE_DOLLAR_VOL
      • Optional short-interest gate if USE_SHORT_INTEREST_FILTER is true:
          - short% of float >= MIN_SHORT_PERCENT
          - days-to-cover >= MIN_DTC
      • Only during 9:30–16:00 EST
    """
    if not POLYGON_KEY:
        print("[squeeze] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[squeeze] Client not initialized; skipping scan.")
        return
    if not _in_squeeze_window():
        print("[squeeze] Outside 9:30–16:00 window; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Daily bars for % move & RVOL
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
        last_price = float(today_bar.close)

        if last_price < MIN_SQUEEZE_PRICE or prev_close <= 0:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
        if move_pct < MIN_SQUEEZE_MOVE_PCT:
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

        # Optional short-interest gate
        si_text = "Short data: N/A"
        if USE_SHORT_INTEREST_FILTER:
            si = get_short_interest(sym)
            if not si:
                # If we strictly require short data, skip when missing
                print(f"[squeeze] Missing short-interest for {sym}; skipping due to USE_SHORT_INTEREST_FILTER.")
                continue

            short_pct, dtc = si
            if short_pct < MIN_SHORT_PERCENT or dtc < MIN_DTC:
                continue

            si_text = f"Short {short_pct:.1f}% · DTC {dtc:.1f} days"

        # Intraday context: position vs HOD
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
            print(f"[squeeze] minute fetch failed for {sym}: {e}")
            mins = []

        if mins:
            day_high = max(float(b.high) for b in mins)
            day_low = min(float(b.low) for b in mins)
        else:
            day_high = float(today_bar.high)
            day_low = float(today_bar.low)

        if day_high > 0:
            from_high_pct = (day_high - last_price) / day_high * 100.0
        else:
            from_high_pct = 0.0

        if abs(from_high_pct) < 1.0:
            hod_text = "at/near HOD"
        else:
            hod_text = f"{from_high_pct:.1f}% below HOD"

        dv = dollar_vol
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        bias = "Nuclear long squeeze candidate" if move_pct > 0 else "Violent downside squeeze / liquidation"

        extra = (
            f"🔥 Short Squeeze Behaviour Detected\n"
            f"📈 Prev Close: ${prev_close:.2f} → Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f} · Close {hod_text}\n"
            f"📊 RVOL: {rvol:.1f}x · Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f})\n"
            f"📌 {si_text}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("squeeze", sym, last_price, rvol, extra=extra)