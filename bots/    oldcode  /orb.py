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
    is_etf_blacklisted,
    grade_equity_setup,
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# -------- CONFIG --------
# Base filters for which symbols are worth scanning for ORB
MIN_ORB_PRICE = float(os.getenv("MIN_ORB_PRICE", "3.0"))          # was 5.0
MIN_ORB_RVOL = float(os.getenv("MIN_ORB_RVOL", "1.8"))            # was 2.5
MIN_ORB_DOLLAR_VOL = float(os.getenv("MIN_ORB_DOLLAR_VOL", "5000000"))  # was 8M+

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


def _in_build_window(now: Optional[datetime] = None) -> bool:
    """Is it currently in the 09:30–09:45 build window? (EST)"""
    if now is None:
        now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return ORB_BUILD_START_MIN <= mins <= ORB_BUILD_END_MIN


def _in_scan_window(now: Optional[datetime] = None) -> bool:
    """Is it currently in the 09:45–11:00 scan window? (EST)"""
    if now is None:
        now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return ORB_SCAN_START_MIN <= mins <= ORB_SCAN_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("ORB_TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.90)


def _fetch_5m_aggs(sym: str, trading_day: date) -> List[Any]:
    """Fetch 5-min intraday bars for the session via Polygon."""
    if not _client:
        return []

    start = trading_day.isoformat()
    end = trading_day.isoformat()

    try:
        aggs = _client.list_aggs(
            sym,
            5,
            "minute",
            start,
            end,
            limit=500,
            sort="asc",
        )
        return list(aggs)
    except Exception as e:
        print(f"[orb] agg error for {sym}: {e}")
        return []


def _filter_session_bars(bars: List[Any], trading_day: date) -> List[Any]:
    """
    Filter intraday 5-min bars to the regular session 09:30–16:00 ET.

    NOTE: We assume Polygon aggregate timestamps are in ms UTC. We
    convert to EST and filter to 09:30–current time in EST.
    """
    filtered: List[Any] = []
    for b in bars:
        ts = getattr(b, "timestamp", getattr(b, "t", None))
        if ts is None:
            continue

        # ms → seconds
        if ts > 1e12:  # ms vs s
            ts = ts / 1000.0

        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        dt_et = dt_utc.astimezone(eastern)

        if dt_et.date() != trading_day:
            continue

        mins = dt_et.hour * 60 + dt_et.minute
        if mins < 9 * 60 + 30 or mins > 16 * 60:
            continue

        # Attach local timestamp for later logic
        b._et = dt_et
        filtered.append(b)

    return filtered


def _compute_day_stats(sym: str, trading_day: date) -> Tuple[float, float, float, float, float]:
    """
    Compute:
      - rvol (intraday vs last 20 days)
      - day_vol
      - last_price
      - prev_close
      - dollar_vol
    """
    if not _client:
        return 1.0, 0.0, 0.0, 0.0, 0.0

    # Today's intraday 5m bars
    bars_5m = _fetch_5m_aggs(sym, trading_day)
    if not bars_5m:
        return 1.0, 0.0, 0.0, 0.0, 0.0

    day_vol = float(sum(getattr(b, "volume", getattr(b, "v", 0)) for b in bars_5m))
    last_price = float(getattr(bars_5m[-1], "close", getattr(bars_5m[-1], "c", 0)) or 0)

    # Daily history (last 21 days including today)
    try:
        start = (trading_day - timedelta(days=30)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=50,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[orb] daily aggs error for {sym}: {e}")
        return 1.0, day_vol, last_price, 0.0, 0.0

    if not daily:
        return 1.0, day_vol, last_price, 0.0, 0.0

    # Last daily bar is "today" (in theory)
    d0 = daily[-1]
    prev_close = float(getattr(daily[-2], "close", getattr(daily[-2], "c", 0))) if len(daily) >= 2 else 0.0

    # Compute 20-day average volume excluding today
    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if not recent:
        avg_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)))
    else:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0))) for d in recent) / len(recent)

    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * day_vol

    return rvol, day_vol, last_price, prev_close, dollar_vol


def _build_orb_range(bars_5m: List[Any]) -> Optional[Tuple[float, float]]:
    """
    Build the 15m ORB from the first 3 × 5m bars after 09:30.
    """
    first_3: List[Any] = []
    for b in bars_5m:
        dt_et = getattr(b, "_et", None)
        if not isinstance(dt_et, datetime):
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if mins < 9 * 60 + 30:
            continue
        if mins >= 9 * 60 + 45:
            continue
        first_3.append(b)

    if len(first_3) < 3:
        return None

    low = min(float(getattr(b, "low", getattr(b, "l", 0))) for b in first_3)
    high = max(float(getattr(b, "high", getattr(b, "h", 0))) for b in first_3)
    return low, high


def _find_breakout_and_retest(
    sym: str,
    orb_low: float,
    orb_high: float,
    bars_5m: List[Any],
) -> Optional[Tuple[str, Any, Any]]:
    """
    Look for breakout after ORB window:
      - For longs: close above ORB high on a 5m bar
      - For shorts: close below ORB low
      - Then an FVG-style retest where price tags ORB level but holds bias.
    """
    breakout_idx = None
    direction = None  # "long" or "short"

    # Pass 1: find breakout
    for i, b in enumerate(bars_5m):
        dt_et = getattr(b, "_et", None)
        if not isinstance(dt_et, datetime):
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if mins < ORB_SCAN_START_MIN:
            continue
        if mins > ORB_SCAN_END_MIN:
            break

        o = float(getattr(b, "open", getattr(b, "o", 0)))
        h = float(getattr(b, "high", getattr(b, "h", 0)))
        l = float(getattr(b, "low", getattr(b, "l", 0)))
        c = float(getattr(b, "close", getattr(b, "c", 0)))

        # Long breakout: candle closes above ORB high
        if c > orb_high and l > orb_low:
            breakout_idx = i
            direction = "long"
            break

        # Short breakout: candle closes below ORB low
        if c < orb_low and h < orb_high:
            breakout_idx = i
            direction = "short"
            break

    if breakout_idx is None or direction is None:
        return None

    breakout_bar = bars_5m[breakout_idx]

    # Pass 2: FVG-ish retest: later bar tags ORB boundary while holding direction
    for j in range(breakout_idx + 1, len(bars_5m)):
        b = bars_5m[j]
        dt_et = getattr(b, "_et", None)
        if not isinstance(dt_et, datetime):
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if mins > ORB_SCAN_END_MIN:
            break

        o = float(getattr(b, "open", getattr(b, "o", 0)))
        h = float(getattr(b, "high", getattr(b, "h", 0)))
        l = float(getattr(b, "low", getattr(b, "l", 0)))
        c = float(getattr(b, "close", getattr(b, "c", 0)))

        if direction == "long":
            # Retest at/near ORB high but hold above ORB low and close green-ish
            if l <= orb_high * 1.002 and c > o and c > orb_high:
                retest_bar = b
                return direction, breakout_bar, retest_bar

        if direction == "short":
            # Retest at/near ORB low but hold below ORB high and close red-ish
            if h >= orb_low * 0.998 and c < o and c < orb_low:
                retest_bar = b
                return direction, breakout_bar, retest_bar

    return None


async def run_orb():
    """
    ORB bot main entrypoint.

    High level:
      1) Only operate between 09:30–11:00 EST.
      2) Compute day RVOL & dollar volume; filter by MIN_ORB_RVOL, MIN_ORB_DOLLAR_VOL.
      3) Build 15m ORB from first 3×5m bars.
      4) Scan for breakout + FVG-style retest.
      5) Alert once per symbol per day.
    """
    _reset_if_new_day()

    now = datetime.now(eastern)
    if not (_in_build_window(now) or _in_scan_window(now)):
        print("[orb] outside ORB windows; skipping.")
        return

    if not POLYGON_KEY or not _client:
        print("[orb] missing POLYGON_KEY or REST client; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[orb] empty universe; skipping.")
        return

    trading_day = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # Fetch intraday bars and compute day stats
        bars_5m = _fetch_5m_aggs(sym, trading_day)
        if not bars_5m:
            continue

        bars_5m = _filter_session_bars(bars_5m, trading_day)
        if not bars_5m:
            continue

        rvol, day_vol, last_price, prev_close, dollar_vol = _compute_day_stats(sym, trading_day)

        if last_price <= 0 or prev_close <= 0:
            continue
        if last_price < MIN_ORB_PRICE:
            continue
        # Use ORB-specific RVOL threshold (looser)
        if rvol < MIN_ORB_RVOL:
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_ORB_DOLLAR_VOL:
            continue

        # Build ORB range
        orb_range = _build_orb_range(bars_5m)
        if not orb_range:
            continue

        orb_low, orb_high = orb_range
        if orb_high <= orb_low:
            continue

        # Check for breakout + retest
        result = _find_breakout_and_retest(sym, orb_low, orb_high, bars_5m)
        if not result:
            continue

        direction, breakout_bar, retest_bar = result

        br_o = float(getattr(breakout_bar, "open", getattr(breakout_bar, "o", 0)))
        br_h = float(getattr(breakout_bar, "high", getattr(breakout_bar, "h", 0)))
        br_l = float(getattr(breakout_bar, "low", getattr(breakout_bar, "l", 0)))
        br_c = float(getattr(breakout_bar, "close", getattr(breakout_bar, "c", 0)))
        br_range = br_h - br_l

        move_pct = (last_price - prev_close) / prev_close * 100.0
        dollar_grade = dollar_vol
        grade = grade_equity_setup(move_pct, rvol, dollar_grade)

        dir_text = "15m ORB LONG" if direction == "long" else "15m ORB SHORT"
        bias = "Bullish breakout with FVG retest" if direction == "long" else "Bearish breakdown with FVG retest"

        body = (
            f"📣 {dir_text} (15m ORB, 5m FVG retest)\n"
            f"📏 ORB Range (first 15m): {orb_low:.2f} – {orb_high:.2f}\n"
            f"🧱 Breakout candle (5m): O {br_o:.2f} · H {br_h:.2f} · "
            f"L {br_l:.2f} · C {br_c:.2f} (range {br_range:.2f})\n"
            f"🔁 FVG-style retest confirmed on later 5m bar while holding ORB edge\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 Day RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        time_str = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 ORB — {sym}\n"
            f"🕒 {time_str}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        _mark_alerted(sym)
        send_alert("orb", sym, last_price, rvol, extra=extra)
