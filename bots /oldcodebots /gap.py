# bots/gap.py

import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Any

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

MIN_GAP_PRICE = float(os.getenv("MIN_GAP_PRICE", "3.0"))
MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "3.0"))
MIN_GAP_RVOL = float(os.getenv("MIN_GAP_RVOL", "1.5"))
MIN_GAP_DOLLAR_VOL = float(os.getenv("MIN_GAP_DOLLAR_VOL", "5000000"))  # $5M+

# Only scan gaps until ~11:00 ET
GAP_SCAN_END_MIN = 11 * 60

# Per-day dedupe
_alert_date: date | None = None
_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    return sym in _alerted_syms


def _mark(sym: str) -> None:
    _alerted_syms.add(sym)


def _in_gap_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    # 09:30 – 11:00 ET
    return 9 * 60 + 30 <= mins <= GAP_SCAN_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("GAP_TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.90)


def _fetch_daily_window(sym: str, trading_day: date):
    """
    Fetch the last two daily candles (yesterday, today) for gap calculations.
    Returns (prev_day, today_day) or (None, None) on failure.
    """
    if not _client:
        return None, None

    try:
        start = (trading_day - timedelta(days=5)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=10,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[gap] daily agg error for {sym}: {e}")
        return None, None

    if len(daily) < 2:
        return None, None

    return daily[-2], daily[-1]


def _compute_gap_stats(sym: str, trading_day: date) -> Optional[Dict[str, Any]]:
    prev_day, today_day = _fetch_daily_window(sym, trading_day)
    if not prev_day or not today_day:
        return None

    prev_close = float(getattr(prev_day, "close", getattr(prev_day, "c", 0)))
    open_today = float(getattr(today_day, "open", getattr(today_day, "o", 0)))
    day_high = float(getattr(today_day, "high", getattr(today_day, "h", 0)))
    day_low = float(getattr(today_day, "low", getattr(today_day, "l", 0)))
    last_price = float(getattr(today_day, "close", getattr(today_day, "c", 0)))
    vol_today = float(getattr(today_day, "volume", getattr(today_day, "v", 0)))

    if prev_close <= 0 or open_today <= 0:
        return None

    gap_pct = (open_today - prev_close) / prev_close * 100.0
    intraday_pct = (last_price - open_today) / open_today * 100.0
    total_move_pct = (last_price - prev_close) / prev_close * 100.0

    # Compute RVOL vs last 20 days (excluding today)
    try:
        start = (trading_day - timedelta(days=40)).isoformat()
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
        print(f"[gap] daily history error for {sym}: {e}")
        return None

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if recent:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0))) for d in recent) / len(recent)
    else:
        avg_vol = vol_today

    rvol = vol_today / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * vol_today

    return {
        "prev_close": prev_close,
        "open_today": open_today,
        "last_price": last_price,
        "day_low": day_low,
        "day_high": day_high,
        "vol_today": vol_today,
        "gap_pct": gap_pct,
        "intraday_pct": intraday_pct,
        "total_move_pct": total_move_pct,
        "rvol": rvol,
        "dollar_vol": dollar_vol,
    }


async def run_gap():
    """
    Regular session gap scanner.

    Looks for:
      • Gappers around the open
      • Sufficient RVOL + dollar volume
      • Single alert per symbol per day
    """
    _reset_if_new_day()

    if not _in_gap_window():
        print("[gap] outside gap scan window; skipping.")
        return

    if not POLYGON_KEY or not _client:
        print("[gap] missing POLYGON_KEY or client; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[gap] empty universe; skipping.")
        return

    trading_day = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        stats = _compute_gap_stats(sym, trading_day)
        if not stats:
            continue

        prev_close = stats["prev_close"]
        open_today = stats["open_today"]
        last_price = stats["last_price"]
        day_low = stats["day_low"]
        day_high = stats["day_high"]
        vol_today = stats["vol_today"]
        gap_pct = stats["gap_pct"]
        intraday_pct = stats["intraday_pct"]
        total_move_pct = stats["total_move_pct"]
        rvol = stats["rvol"]
        dollar_vol = stats["dollar_vol"]

        if last_price < MIN_GAP_PRICE:
            continue

        if abs(gap_pct) < MIN_GAP_PCT:
            continue

        # RVOL gate: use max of bot-specific and global
        if rvol < max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL):
            continue

        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        if dollar_vol < MIN_GAP_DOLLAR_VOL:
            continue

        direction = "Gap Up" if gap_pct > 0 else "Gap Down"
        emoji = "🚀" if gap_pct > 0 else "🩸"

        grade = grade_equity_setup(total_move_pct, rvol, dollar_vol)
        if gap_pct > 0:
            if intraday_pct > 0:
                bias = "Gap-and-go strength intraday"
            elif intraday_pct < 0:
                bias = "Gap fading intraday"
            else:
                bias = "Holding the gap so far"
        else:
            if intraday_pct < 0:
                bias = "Gap-down continuation lower"
            elif intraday_pct > 0:
                bias = "Gap-down bounce attempt"
            else:
                bias = "Holding the downside gap so far"

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

        time_str = now_est()
        if not isinstance(time_str, str):
            time_str = time_str.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 GAP — {sym}\n"
            f"🕒 {time_str}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        _mark(sym)
        send_alert("gap", sym, last_price, rvol, extra=extra)
