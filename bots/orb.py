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

# Loosened so ORB will actually fire more:
MIN_ORB_PRICE = float(os.getenv("MIN_ORB_PRICE", "3.0"))
MIN_ORB_RVOL = float(os.getenv("MIN_ORB_RVOL", "1.8"))
MIN_ORB_DOLLAR_VOL = float(os.getenv("MIN_ORB_DOLLAR_VOL", "5000000"))  # $5M+

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
    return sym in _alerted_syms


def _mark_alerted(sym: str) -> None:
    _alerted_syms.add(sym)


def _now_minutes_et() -> int:
    now = datetime.now(eastern)
    return now.hour * 60 + now.minute


def _in_build_window() -> bool:
    m = _now_minutes_et()
    return ORB_BUILD_START_MIN <= m <= ORB_BUILD_END_MIN


def _in_scan_window() -> bool:
    m = _now_minutes_et()
    return ORB_SCAN_START_MIN <= m <= ORB_SCAN_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    # Slightly larger for ORB
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


def _fetch_5m_aggs(sym: str, day: date) -> List[Any]:
    """Get all 5-minute bars for the day."""
    if not _client:
        return []

    day_s = day.isoformat()
    try:
        aggs = _client.list_aggs(
            sym,
            5,
            "minute",
            day_s,
            day_s,
            limit=500,
            sort="asc",
        )
        return list(aggs)
    except Exception as e:
        print(f"[orb] 5m aggs error for {sym}: {e}")
        return []


def _fetch_1m_aggs(sym: str, day: date) -> List[Any]:
    """Get all 1-minute bars for the day (for refinement / confirmation if needed)."""
    if not _client:
        return []

    day_s = day.isoformat()
    try:
        aggs = _client.list_aggs(
            sym,
            1,
            "minute",
            day_s,
            day_s,
            limit=1500,
            sort="asc",
        )
        return list(aggs)
    except Exception as e:
        print(f"[orb] 1m aggs error for {sym}: {e}")
        return []


def _build_orb_range_5m(bars_5m: List[Any]) -> Optional[Tuple[float, float]]:
    """
    Given the day's 5m bars in ascending order, build the opening 15-min range
    from the first 3 bars that start at 9:30 ET.
    """

    if not bars_5m:
        return None

    orb_bars: List[Any] = []
    for bar in bars_5m:
        # Polygon massive vs polygon client differences; handle generically.
        ts = getattr(bar, "timestamp", None) or getattr(bar, "t", None)
        if ts is None:
            continue

        # ts may be ms since epoch
        try:
            ts_dt = datetime.fromtimestamp(ts / 1000.0, tz=eastern)
        except Exception:
            continue

        if ts_dt.hour == 9 and ts_dt.minute in (30, 35, 40):
            orb_bars.append(bar)

    if len(orb_bars) < 3:
        return None

    highs = [float(getattr(b, "high", getattr(b, "h", 0.0))) for b in orb_bars]
    lows = [float(getattr(b, "low", getattr(b, "l", 0.0))) for b in orb_bars]

    orb_high = max(highs)
    orb_low = min(lows)

    return orb_low, orb_high


def _day_rvol_and_volume(sym: str, day: date) -> Optional[Tuple[float, float, float, float]]:
    """
    Compute day-level RVOL, volume, dollar volume, last price, prev close.
    """

    if not _client:
        return None

    # Today + prior n days (for RVOL baseline)
    start = day - timedelta(days=15)
    end = day
    try:
        aggs = _client.list_aggs(
            sym,
            1,
            "day",
            start.isoformat(),
            end.isoformat(),
            limit=50,
            sort="asc",
        )
        days = list(aggs)
    except Exception as e:
        print(f"[orb] daily aggs error for {sym}: {e}")
        return None

    if len(days) < 2:
        return None

    d0 = days[-1]  # today
    d1 = days[-2]  # yesterday

    last_price = float(getattr(d0, "close", getattr(d0, "c", 0.0)))
    prev_close = float(getattr(d1, "close", getattr(d1, "c", 0.0)))

    if last_price <= 0 or prev_close <= 0:
        return None

    day_vol = float(getattr(d0, "volume", getattr(d0, "v", 0.0)))
    hist = days[:-1]
    recent = hist[-20:] if len(hist) > 20 else hist
    avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0.0))) for d in recent) / len(recent)
    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

    dollar_vol = last_price * day_vol

    return last_price, prev_close, rvol, day_vol, dollar_vol


def _detect_orb_breakout(
    sym: str,
    bars_5m: List[Any],
    orb_low: float,
    orb_high: float,
    last_price: float,
) -> Optional[Tuple[str, float, float]]:
    """
    Simple ORB breakout logic:
      • If we trade above orb_high by at least 0.25% and close > orb_high → bullish breakout.
      • If we trade below orb_low by at least 0.25% and close < orb_low → bearish breakdown.
    Returns (bias, breakout_level, last_price).
    """

    if not bars_5m:
        return None

    # Just use the last 5m bar as of now
    last_bar = bars_5m[-1]
    high = float(getattr(last_bar, "high", getattr(last_bar, "h", 0.0)))
    low = float(getattr(last_bar, "low", getattr(last_bar, "l", 0.0)))
    close = float(getattr(last_bar, "close", getattr(last_bar, "c", 0.0)))

    up_trigger = orb_high * 1.0025  # 0.25% above high
    down_trigger = orb_low * 0.9975  # 0.25% below low

    if high >= up_trigger and close > orb_high:
        return "Bullish ORB breakout", orb_high, last_price

    if low <= down_trigger and close < orb_low:
        return "Bearish ORB breakdown", orb_low, last_price

    return None


async def run_orb():
    """
    Opening Range Breakout bot.

    - Builds 15-min ORB from first 3×5m bars (9:30–9:45).
    - Scans for breakouts 9:45–11:00.
    - Requires:
        • Price >= MIN_ORB_PRICE
        • Day RVOL >= max(MIN_ORB_RVOL, MIN_RVOL_GLOBAL)
        • Day volume >= MIN_VOLUME_GLOBAL
        • Day dollar volume >= MIN_ORB_DOLLAR_VOL
    """

    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[orb] no API key; skipping.")
        return

    if not (_in_build_window() or _in_scan_window()):
        print("[orb] outside ORB window; skipping.")
        return

    today = date.today()
    universe = _get_universe()
    if not universe:
        print("[orb] empty universe; skipping.")
        return

    for sym in universe:
        if _already_alerted(sym):
            continue
        if is_etf_blacklisted(sym):
            continue

        # Day-level filters
        day_info = _day_rvol_and_volume(sym, today)
        if not day_info:
            continue

        last_price, prev_close, rvol, day_vol, dollar_vol = day_info

        if last_price < MIN_ORB_PRICE:
            continue
        if rvol < max(MIN_ORB_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_ORB_DOLLAR_VOL:
            continue

        # Build ORB from 5m bars
        bars_5m = _fetch_5m_aggs(sym, today)
        if not bars_5m:
            continue

        orb_range = _build_orb_range_5m(bars_5m)
        if not orb_range:
            continue

        orb_low, orb_high = orb_range
        if orb_low <= 0 or orb_high <= 0:
            continue

        # If we are still in build window, we just skip alerts for now.
        if _in_build_window():
            continue

        # In scan window, look for breakout
        breakout = _detect_orb_breakout(sym, bars_5m, orb_low, orb_high, last_price)
        if not breakout:
            continue

        bias, breakout_level, _ = breakout

        move_pct = (last_price - prev_close) / prev_close * 100.0
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        body = (
            f"🧱 ORB Range: {orb_low:.2f} → {orb_high:.2f}\n"
            f"📍 Breakout Level: {breakout_level:.2f}\n"
            f"💰 Price: ${last_price:.2f} (move {move_pct:.1f}% vs prev close)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 Day RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        # Nice formatted EST timestamp
        ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 ORB — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        _mark_alerted(sym)
        send_alert("orb", sym, last_price, rvol, extra=extra)