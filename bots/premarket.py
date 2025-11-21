import os
from datetime import date, timedelta, datetime
from typing import List, Tuple, Optional, Any

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

# ---- Config / thresholds ----

MIN_PREMARKET_PRICE = float(os.getenv("MIN_PREMARKET_PRICE", "2.0"))
MIN_PREMARKET_MOVE_PCT = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "6.0"))  # vs prior close
MIN_PREMARKET_DOLLAR_VOL = float(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "3000000"))  # $3M+
MIN_PREMARKET_RVOL = float(os.getenv("MIN_PREMARKET_RVOL", "2.0"))

# Premarket scan window 04:00–09:29 ET
PREMARKET_START_MIN = 4 * 60
PREMARKET_END_MIN = 9 * 60 + 29

# Per-day de-dupe
_alerted_date: Optional[date] = None
_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _alerted_date, _alerted_syms
    today = date.today()
    if _alerted_date != today:
        _alerted_date = today
        _alerted_syms = set()


def _already(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted_syms


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _alerted_syms.add(sym)


def _in_premarket_window() -> bool:
    """Only run 04:00–09:29 ET (premarket)."""
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return PREMARKET_START_MIN <= mins <= PREMARKET_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    # dynamic: top ~100 tickers that cover ~90% of volume
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _get_prev_and_today(sym: str):
    today = date.today()
    today_s = today.isoformat()
    try:
        days = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=(today - timedelta(days=40)).isoformat(),
                to=today_s,
                limit=40,
            )
        )
    except Exception as e:
        print(f"[premarket] daily fetch failed for {sym}: {e}")
        return None, None

    if len(days) < 2:
        return None, None

    return days[-2], days[-1]


def _get_bar_timestamp_et(bar: Any) -> Optional[datetime]:
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
        # Polygon uses ms; convert to seconds
        dt_utc = datetime.fromtimestamp(ts / 1000.0, tz=pytz.UTC)
        return dt_utc.astimezone(eastern)
    except Exception:
        return None


def _get_premarket_window_aggs(sym: str) -> Tuple[float, float, float, float]:
    """
    Returns (pre_low, pre_high, pre_last, pre_volume) for 1m bars between 04:00–09:29.

    IMPORTANT: Polygon complained about ISO datetimes in 'from'.
    To keep it happy, we request the whole **day** using just YYYY-MM-DD and
    then filter 04:00–09:29 ET in Python based on each bar's timestamp.
    """
    today = date.today()
    day_str = today.isoformat()

    try:
        bars = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="minute",
                from_=day_str,  # YYYY-MM-DD (Polygon-friendly)
                to=day_str,
                limit=2000,
            )
        )
    except Exception as e:
        print(f"[premarket] minute fetch failed for {sym}: {e}")
        return 0.0, 0.0, 0.0, 0.0

    if not bars:
        return 0.0, 0.0, 0.0, 0.0

    pre_bars = []
    for b in bars:
        dt_et = _get_bar_timestamp_et(b)
        if not dt_et:
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if PREMARKET_START_MIN <= mins <= PREMARKET_END_MIN:
            pre_bars.append(b)

    if not pre_bars:
        return 0.0, 0.0, 0.0, 0.0

    lows = [float(b.low) for b in pre_bars]
    highs = [float(b.high) for b in pre_bars]
    vols = [float(b.volume) for b in pre_bars]

    pre_low = min(lows)
    pre_high = max(highs)
    pre_last = float(pre_bars[-1].close)
    pre_vol = sum(vols)

    return pre_low, pre_high, pre_last, pre_vol


async def run_premarket():
    """
    Premarket Runner Bot:

      • Runs only 04:00–09:29 ET.
      • Universe: dynamic top-volume list (or TICKER_UNIVERSE if provided).
      • Requirements:
          - Price >= MIN_PREMARKET_PRICE
          - |Move vs prior close| >= MIN_PREMARKET_MOVE_PCT
          - Premarket dollar volume >= MIN_PREMARKET_DOLLAR_VOL
          - Day RVOL (partial) >= max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL)
          - Day volume so far >= MIN_VOLUME_GLOBAL
      • Per symbol: only one alert per day.
    """
    if not POLYGON_KEY or not _client:
        print("[premarket] No POLYGON_KEY/client; skipping.")
        return

    if not _in_premarket_window():
        print("[premarket] Outside premarket window; skipping.")
        return

    _reset_if_new_day()

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        prev_bar, today_bar = _get_prev_and_today(sym)
        if not prev_bar or not today_bar:
            continue

        prev_close = float(prev_bar.close)
        if prev_close <= 0:
            continue

        # Day volume so far (partial, because it's still premarket)
        todays_partial_vol = float(today_bar.volume)

        # Premarket minute bars
        pre_low, pre_high, last_px, pre_vol = _get_premarket_window_aggs(sym)
        if last_px <= 0 or pre_vol <= 0:
            continue

        if last_px < MIN_PREMARKET_PRICE:
            continue

        move_pct = (last_px - prev_close) / prev_close * 100.0
        if abs(move_pct) < MIN_PREMARKET_MOVE_PCT:
            continue

        pre_dollar_vol = last_px * pre_vol
        if pre_dollar_vol < MIN_PREMARKET_DOLLAR_VOL:
            continue

        # Day RVOL (partial)
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=40,
                )
            )
        except Exception as e:
            print(f"[premarket] extra daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        if avg_vol > 0:
            rvol = todays_partial_vol / avg_vol
        else:
            rvol = 1.0

        if rvol < max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL):
            continue

        if todays_partial_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol_day_partial = last_px * todays_partial_vol

        # Grade & bias
        gap_pct = move_pct
        grade = grade_equity_setup(abs(gap_pct), rvol, dollar_vol_day_partial)

        direction = "up" if move_pct > 0 else "down"
        emoji = "🚀" if move_pct > 0 else "⚠️"
        bias = (
            "Long premarket momentum"
            if move_pct > 0
            else "Watch for continuation / short setup on weakness"
        )

        body = (
            f"{emoji} Premarket move: {move_pct:.1f}% {direction} vs prior close\n"
            f"📈 Prev Close: ${prev_close:.2f} → Premarket Last: ${last_px:.2f}\n"
            f"📏 Premarket Range: ${pre_low:.2f} – ${pre_high:.2f}\n"
            f"📦 Premarket Volume: {int(pre_vol):,} (≈ ${pre_dollar_vol:,.0f})\n"
            f"📊 Day RVOL (partial): {rvol:.1f}x · Day Vol (so far): {int(todays_partial_vol):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)

        extra = (
            f"📣 PREMARKET — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_px:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("premarket", sym, last_px, rvol, extra=extra)
