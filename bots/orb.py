# bots/orb.py — Opening Range Breakout (15m ORB, 5m FVG retest)

import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple, Any

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

# ---------------- CONFIG ----------------

MIN_ORB_PRICE = float(os.getenv("MIN_ORB_PRICE", "5.0"))
MIN_ORB_RVOL = float(os.getenv("MIN_ORB_RVOL", "2.5"))
MIN_ORB_DOLLAR_VOL = float(os.getenv("MIN_ORB_DOLLAR_VOL", "8000000"))  # $8M+

# ORB timing (EST)
# 9:30–9:45 → build 15-min range (first 3×5m bars)
# 9:45–11:00 → look for breakout + FVG-style retest
ORB_BUILD_START_MIN = 9 * 60 + 30
ORB_BUILD_END_MIN = 9 * 60 + 45
ORB_SCAN_START_MIN = 9 * 60 + 45
ORB_SCAN_END_MIN = 11 * 60

# Per-day de-dupe
_alert_date: Optional[date] = None
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


def _in_orb_window() -> bool:
    """Only run ORB scan between 09:45 and 11:00 EST on weekdays."""
    now_et = datetime.now(eastern)
    if now_et.weekday() >= 5:  # 0=Mon, 6=Sun
        return False
    mins = now_et.hour * 60 + now_et.minute
    return ORB_SCAN_START_MIN <= mins <= ORB_SCAN_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _fetch_day_bars(sym: str, today: date):
    """Fetch last ~40 daily bars for RVOL + prev close."""
    today_s = today.isoformat()
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
        return None
    if len(days) < 2:
        return None
    return days


def _compute_rvol(days) -> Tuple[float, float, float, float]:
    """Return (rvol, day_vol, last_price, prev_close)."""
    today_bar = days[-1]
    prev_bar = days[-2]

    last_price = float(today_bar.close)
    prev_close = float(prev_bar.close)
    day_vol = float(today_bar.volume)

    hist = days[:-1]
    if hist:
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = float(sum(d.volume for d in recent)) / len(recent)
    else:
        avg_vol = day_vol

    if avg_vol > 0:
        rvol = day_vol / avg_vol
    else:
        rvol = 1.0

    return rvol, day_vol, last_price, prev_close


def _bar_timestamp_et(bar: Any) -> Optional[datetime]:
    """
    Convert Polygon agg bar timestamp (ms) to a timezone-aware datetime in EST.
    Polygon v2 aggs typically expose 'timestamp' or 't' in milliseconds since epoch.
    """
    ts = getattr(bar, "timestamp", None)
    if ts is None:
        ts = getattr(bar, "t", None)
    if ts is None:
        return None
    try:
        dt_utc = datetime.fromtimestamp(ts / 1000.0, tz=pytz.UTC)
        return dt_utc.astimezone(eastern)
    except Exception:
        return None


def _fetch_5m_bars(sym: str, today: date) -> List[Any]:
    """
    Fetch today's 5-minute bars and filter to 09:30–current time in EST.

    IMPORTANT:
      Polygon intraday 'from_' / 'to' must be either a DATE (YYYY-MM-DD)
      or a Unix timestamp. We avoid ISO datetimes and request the whole day
      via YYYY-MM-DD, then filter by local time.
    """
    day_str = today.isoformat()

    try:
        all_bars = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=5,
                timespan="minute",
                from_=day_str,  # YYYY-MM-DD (Polygon-compatible)
                to=day_str,
                limit=500,
            )
        )
    except Exception as e:
        print(f"[orb] 5m fetch failed for {sym}: {e}")
        return []

    if not all_bars:
        return []

    now_et = datetime.now(eastern)
    now_mins = now_et.hour * 60 + now_et.minute

    filtered: List[Any] = []
    for b in all_bars:
        dt_et = _bar_timestamp_et(b)
        if not dt_et:
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        # Keep bars from 09:30 up to current time
        if ORB_BUILD_START_MIN <= mins <= now_mins:
            filtered.append(b)

    return filtered


def _build_orb_range(bars: List[Any]) -> Optional[Tuple[float, float]]:
    """Compute ORB high/low from first 3×5m bars (9:30–9:45)."""
    if len(bars) < 3:
        return None
    first_three = bars[:3]
    orb_low = min(float(b.low) for b in first_three)
    orb_high = max(float(b.high) for b in first_three)
    return orb_low, orb_high


def _find_breakout_and_retest(
    bars: List[Any], orb_low: float, orb_high: float
) -> Optional[Tuple[str, Any, Any]]:
    """
    Return (direction, breakout_bar, retest_bar) or None.

    direction: "up" for breakout above orb_high, "down" for breakdown below orb_low.

    Logic:
      • Find the first clean breakout/breakdown bar after the ORB (index >=3).
      • Then look for a later bar that "retests" the ORB edge while respecting the breakout.
    """
    breakout_idx: Optional[int] = None
    direction: Optional[str] = None

    # Skip first 3 bars (used for ORB build)
    for i in range(3, len(bars)):
        b = bars[i]
        close = float(b.close)
        high = float(b.high)
        low = float(b.low)

        # Clean breakout above ORB high
        if close > orb_high and low >= orb_low:
            breakout_idx = i
            direction = "up"
            break

        # Clean breakdown below ORB low
        if close < orb_low and high <= orb_high:
            breakout_idx = i
            direction = "down"
            break

    if breakout_idx is None or direction is None:
        return None

    # FVG-style retest: later bar tags ORB edge but still respects the breakout
    for j in range(breakout_idx + 1, len(bars)):
        r = bars[j]
        r_high = float(r.high)
        r_low = float(r.low)
        r_close = float(r.close)

        if direction == "up":
            # Retest down into orb_high, close back above
            if r_low <= orb_high <= r_high and r_close >= orb_high:
                return direction, bars[breakout_idx], r
        else:
            # direction == "down"
            if r_low <= orb_low <= r_high and r_close <= orb_low:
                return direction, bars[breakout_idx], r

    return None


async def run_orb():
    """
    Opening Range Breakout (ORB) Bot with 5m FVG-style retest:

      • 09:30–09:45 → build 15-min ORB (first three 5m bars).
      • 09:45–11:00 → look for first clean breakout/breakdown of the ORB edge.
      • Require a later 5m bar that retests the ORB edge (FVG-style) while holding it.
      • Filters:
          - Price >= MIN_ORB_PRICE
          - Day RVOL >= max(MIN_ORB_RVOL, MIN_RVOL_GLOBAL)
          - Day volume >= MIN_VOLUME_GLOBAL
          - Day dollar volume >= MIN_ORB_DOLLAR_VOL
      • One alert per symbol per day.
    """
    if not POLYGON_KEY or not _client:
        print("[orb] POLYGON_KEY not set or client not initialized; skipping.")
        return
    if not _in_orb_window():
        print("[orb] Outside ORB scan window; skipping.")
        return

    _reset_if_new_day()
    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # Daily context: RVOL, prev close, etc.
        days = _fetch_day_bars(sym, today)
        if not days:
            continue

        rvol, day_vol, last_price, prev_close = _compute_rvol(days)
        if last_price <= 0 or prev_close <= 0:
            continue
        if last_price < MIN_ORB_PRICE:
            continue
        if rvol < max(MIN_ORB_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_ORB_DOLLAR_VOL:
            continue

        # 5m structure
        bars_5m = _fetch_5m_bars(sym, today)
        if len(bars_5m) < 5:
            continue

        orb_range = _build_orb_range(bars_5m)
        if not orb_range:
            continue
        orb_low, orb_high = orb_range

        breakout_info = _find_breakout_and_retest(bars_5m, orb_low, orb_high)
        if not breakout_info:
            continue

        direction, br_bar, _retest_bar = breakout_info

        br_open = float(br_bar.open)
        br_high = float(br_bar.high)
        br_low = float(br_bar.low)
        br_close = float(br_bar.close)
        br_range = br_high - br_low

        # Use latest bar's close as "current" last_price for alert text
        last_price = float(bars_5m[-1].close)
        move_pct = (last_price - prev_close) / prev_close * 100.0

        if direction == "up":
            emoji = "🚀"
            dir_text = "ORB BREAKOUT UP"
            bias = "Long continuation setup above ORB high"
        else:
            emoji = "⚠️"
            dir_text = "ORB BREAKDOWN DOWN"
            bias = "Short continuation setup below ORB low"

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        body = (
            f"{emoji} {dir_text} (15m ORB, 5m FVG retest)\n"
            f"📏 ORB Range (first 15m): {orb_low:.2f} – {orb_high:.2f}\n"
            f"🧱 Breakout candle (5m): O {br_open:.2f} · H {br_high:.2f} · "
            f"L {br_low:.2f} · C {br_close:.2f} (range {br_range:.2f})\n"
            f"🔁 FVG-style retest confirmed on later 5m bar while holding ORB edge\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 Day RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 ORB — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        _mark_alerted(sym)
        send_alert("orb", sym, last_price, rvol, extra=extra)