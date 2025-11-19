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

# --- ORB + FVG style config ---

# ORB: first 15 minutes of RTH, built from 5-min bars → first 3 bars
ORB_15M_BARS = 3

# Require opening range to be meaningful vs price (avoid tiny/noise ranges)
ORB_MIN_RANGE_PCT = float(os.getenv("ORB_MIN_RANGE_PCT", "0.5"))  # 0.5% of price

# Require breakout candle to extend some fraction of the ORB range beyond the edge
ORB_MIN_BREAK_FRAC = float(os.getenv("ORB_MIN_BREAK_FRAC", "0.25"))  # 25% of range

# FVG-style retest zone inside the breakout candle (fraction of its own range)
# Example: 0.3–0.8 → look for a 30–80% retrace into the impulse candle
FVG_RETEST_MIN_RETRACE = float(os.getenv("FVG_RETEST_MIN_RETRACE", "0.3"))
FVG_RETEST_MAX_RETRACE = float(os.getenv("FVG_RETEST_MAX_RETRACE", "0.8"))

# Time window: only look for ORB + FVG plays in the morning
# (you can widen this if you want)
def _in_orb_window() -> bool:
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    # 9:45–13:00 EST
    return 9 * 60 + 45 <= minutes <= 13 * 60


# --- per-day de-dupe so you only get one ORB/FVG alert per symbol per day ---

_alert_date: date | None = None
_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted_syms


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _alerted_syms.add(sym)


# --- helpers ---

def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _filter_rth_5min(bars) -> List:
    """
    Keep only 5-minute bars during regular hours (9:30–16:00 ET),
    sorted earliest → latest.
    """
    out = []
    for b in bars:
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        dt_utc = datetime.utcfromtimestamp(ts / 1000.0).replace(tzinfo=pytz.UTC)
        dt_et = dt_utc.astimezone(eastern)
        minutes = dt_et.hour * 60 + dt_et.minute
        if 9 * 60 + 30 <= minutes <= 16 * 60:
            out.append((dt_et, b))
    out.sort(key=lambda x: x[0])
    return [b for _, b in out]


def _compute_orb_range(bars_5: List) -> Optional[Tuple[float, float]]:
    if len(bars_5) < ORB_15M_BARS:
        return None
    orb_slice = bars_5[:ORB_15M_BARS]
    orb_high = max(float(b.high) for b in orb_slice)
    orb_low = min(float(b.low) for b in orb_slice)
    if orb_high <= 0 or orb_low <= 0 or orb_high <= orb_low:
        return None
    return orb_low, orb_high


def _detect_breakout_and_fvg_retest(
    bars_5: List, orb_low: float, orb_high: float
) -> Optional[Tuple[str, object, object]]:
    """
    Walk 5-min bars AFTER the first 15m (ORB_15M_BARS),
    look for:
      1) First clean breakout candle
      2) Subsequent candle that retests the breakout candle's "FVG zone"

    Returns: (direction, breakout_bar, retest_bar) or None
    direction: "long" (breakout above ORB high) or "short" (below ORB low)
    """
    if len(bars_5) <= ORB_15M_BARS + 1:
        return None

    opening_range = bars_5[:ORB_15M_BARS]
    rest = bars_5[ORB_15M_BARS:]

    range_size = orb_high - orb_low
    mid_price = (orb_high + orb_low) / 2.0
    if mid_price <= 0:
        return None

    range_pct_vs_price = range_size / mid_price * 100.0
    if range_pct_vs_price < ORB_MIN_RANGE_PCT:
        # range too tiny relative to price → ignore
        return None

    breakout_direction = None
    breakout_bar = None
    zone_low = zone_high = None

    # 1) find first breakout candle beyond the ORB edge
    for b in rest:
        high = float(b.high)
        low = float(b.low)
        close = float(b.close)
        open_ = float(b.open)

        # bullish breakout
        if high > orb_high:
            # how far did it push beyond the edge?
            dist = high - orb_high
            if dist >= ORB_MIN_BREAK_FRAC * range_size and close > open_:
                breakout_direction = "long"
                breakout_bar = b
                # define FVG-style retest zone inside this impulse candle
                br_high = high
                br_low = low
                br_range = max(br_high - br_low, 0.01)
                zone_low = br_low + FVG_RETEST_MIN_RETRACE * br_range
                zone_high = br_low + FVG_RETEST_MAX_RETRACE * br_range
                break

        # bearish breakout
        if low < orb_low:
            dist = orb_low - low
            if dist >= ORB_MIN_BREAK_FRAC * range_size and close < open_:
                breakout_direction = "short"
                breakout_bar = b
                br_high = high
                br_low = low
                br_range = max(br_high - br_low, 0.01)
                # for shorts, FVG zone is a retrace up into the candle body
                zone_high = br_high - FVG_RETEST_MIN_RETRACE * br_range
                zone_low = br_high - FVG_RETEST_MAX_RETRACE * br_range
                break

    if breakout_direction is None or breakout_bar is None:
        return None

    # 2) look for first 5m bar AFTER breakout that tags the zone
    found_breakout = False
    for b in rest:
        if not found_breakout:
            # wait until we hit the breakout bar in the loop
            if b is breakout_bar:
                found_breakout = True
            continue

        low = float(b.low)
        high = float(b.high)
        close = float(b.close)

        if breakout_direction == "long":
            # retest: wick into zone, close still above ORB high
            if (zone_low <= low <= zone_high or zone_low <= high <= zone_high or
                (low < zone_low and high > zone_high)):
                if close > orb_high:
                    return "long", breakout_bar, b

        else:  # short
            if (zone_low <= low <= zone_high or zone_low <= high <= zone_high or
                (low < zone_low and high > zone_high)):
                if close < orb_low:
                    return "short", breakout_bar, b

    return None


# --- main ORB + FVG bot ---

async def run_orb():
    """
    ORB + FVG Retest Bot:

      • Use first 15 minutes of RTH (3×5m bars) as opening range.
      • Require range to be at least ORB_MIN_RANGE_PCT of price.
      • Look for first strong breakout candle:
          - Breaks above ORB high or below ORB low
          - Extends at least ORB_MIN_BREAK_FRAC * range beyond edge
          - Candle body in direction of breakout (close > open for long, < open for short).
      • Define a "FVG-style" retrace zone inside that breakout candle
          - Between FVG_RETEST_MIN_RETRACE and FVG_RETEST_MAX_RETRACE of its own range.
      • Wait for a later 5-min bar to:
          - Retest that zone
          - AND still respect the ORB edge (close above high for longs, below low for shorts).
      • Only then send an alert (one per symbol per day).
    """
    if not POLYGON_KEY:
        print("[orb] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[orb] Client not initialized; skipping scan.")
        return
    if not _in_orb_window():
        print("[orb] Outside ORB window; skipping scan.")
        return

    _reset_if_new_day()
    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # 5-min bars for intraday ORB logic
        try:
            bars_5_all = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=5,
                    timespan="minute",
                    from_=today_s,
                    to=today_s,
                    limit=1000,
                )
            )
        except Exception as e:
            print(f"[orb] 5m fetch failed for {sym}: {e}")
            continue

        bars_5 = _filter_rth_5min(bars_5_all)
        if len(bars_5) < ORB_15M_BARS + 2:
            continue

        orb_range = _compute_orb_range(bars_5)
        if orb_range is None:
            continue

        orb_low, orb_high = orb_range

        # Daily bars for RVOL / volume context
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
            print(f"[orb] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_day = days[-1]
        prev_day = days[-2]

        prev_close = float(prev_day.close)
        last_price = float(today_day.close)

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

        day_vol = float(today_day.volume)
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # detect breakout + FVG retest
        result = _detect_breakout_and_fvg_retest(bars_5, orb_low, orb_high)
        if not result:
            continue

        direction, breakout_bar, retest_bar = result

        move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
        dv = last_price * day_vol
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        br_high = float(breakout_bar.high)
        br_low = float(breakout_bar.low)
        br_open = float(breakout_bar.open)
        br_close = float(breakout_bar.close)
        br_range = br_high - br_low

        if direction == "long":
            emoji = "🚀"
            bias = "Long ORB breakout + FVG retest"
            dir_text = "Breakout ABOVE opening range"
        else:
            emoji = "📉"
            bias = "Short ORB breakdown + FVG retest"
            dir_text = "Breakdown BELOW opening range"

        extra = (
            f"{emoji} {dir_text} (15m ORB, 5m FVG retest)\n"
            f"📏 ORB Range (first 15m): {orb_low:.2f} – {orb_high:.2f}\n"
            f"🧱 Breakout candle (5m): O {br_open:.2f} · H {br_high:.2f} · L {br_low:.2f} · C {br_close:.2f} "
            f"(range {br_range:.2f})\n"
            f"🔁 FVG-style retest confirmed on later 5m bar while holding ORB edge\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)
        send_alert("orb", sym, last_price, rvol, extra=extra)