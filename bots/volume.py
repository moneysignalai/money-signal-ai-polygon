# bots/volume.py — FIXED, PREMIUM FORMAT, WORKING VERSION (2025)

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
    is_etf_blacklisted,
    grade_equity_setup,
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ------- CONFIG -------
MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "2000000"))   # was 8000000
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "12000000"))  # was 30000000
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))

# Per-symbol RVOL threshold for the day (volume bot specific)
MIN_VOLUME_RVOL = float(os.getenv("VOLUME_MIN_RVOL", "1.8"))

# Per-day de-dupe
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


# ------- RTH WINDOW -------
def _in_volume_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)  # 09:30–16:00 ET


# ------- Universe -------
def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


def _fetch_intraday(sym: str, trading_day: date) -> List:
    if not _client:
        return []
    try:
        aggs = _client.list_aggs(
            sym,
            1,
            "minute",
            trading_day.isoformat(),
            trading_day.isoformat(),
            limit=800,
            sort="asc",
        )
        bars = list(aggs)
    except Exception as e:
        print(f"[volume] intraday agg error for {sym}: {e}")
        return []

    # Filter to RTH 09:30–16:00
    filtered = []
    for b in bars:
        ts = getattr(b, "timestamp", getattr(b, "t", None))
        if ts is None:
            continue
        if ts > 1e12:  # ms → s
            ts = ts / 1000.0
        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        dt_et = dt_utc.astimezone(eastern)
        if dt_et.date() != trading_day:
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if mins < 9 * 60 + 30 or mins > 16 * 60:
            continue
        b._et = dt_et
        filtered.append(b)
    return filtered


def _compute_rvol_and_day_stats(sym: str, trading_day: date):
    """Return (rvol, day_vol, last_price, prev_close, dollar_vol)."""
    if not _client:
        return 1.0, 0.0, 0.0, 0.0, 0.0

    # Intraday minute bars for day volume / last price
    bars = _fetch_intraday(sym, trading_day)
    if not bars:
        return 1.0, 0.0, 0.0, 0.0, 0.0

    day_vol = float(sum(getattr(b, "volume", getattr(b, "v", 0)) for b in bars))
    last_price = float(getattr(bars[-1], "close", getattr(bars[-1], "c", 0)) or 0)

    # Daily history
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
        print(f"[volume] daily agg error for {sym}: {e}")
        return 1.0, day_vol, last_price, 0.0, 0.0

    if not daily:
        return 1.0, day_vol, last_price, 0.0, 0.0

    d0 = daily[-1]
    prev_close = float(getattr(daily[-2], "close", getattr(daily[-2], "c", 0))) if len(daily) >= 2 else 0.0

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if not recent:
        avg_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)))
    else:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0))) for d in recent) / len(recent)

    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * day_vol
    return rvol, day_vol, last_price, prev_close, dollar_vol


def _find_monster_bar(sym: str, bars: List, last_price: float) -> tuple[bool, float]:
    """
    Look for a "monster" volume bar intraday relative to others.

    Returns (found, monster_bar_vol).
    """
    if not bars or last_price <= 0:
        return False, 0.0

    vols = [float(getattr(b, "volume", getattr(b, "v", 0))) for b in bars]
    if not vols:
        return False, 0.0

    max_bar_vol = max(vols)
    dollar_bar = max_bar_vol * last_price

    if max_bar_vol < MIN_MONSTER_BAR_SHARES:
        return False, max_bar_vol
    if dollar_bar < MIN_MONSTER_DOLLAR_VOL:
        return False, max_bar_vol

    return True, max_bar_vol


# ------- MAIN BOT -------
async def run_volume():
    """
    Volume Monster Bot:

      • Uses dynamic universe (top volume names) unless TICKER_UNIVERSE is set.
      • Filters by:
          - Day RVOL >= MIN_VOLUME_RVOL (default 1.8x)
          - Day volume >= MIN_VOLUME_GLOBAL
          - Dollar volume >= MIN_MONSTER_DOLLAR_VOL
          - At least one intraday bar with:
              * volume >= MIN_MONSTER_BAR_SHARES
              * bar-dollar-vol >= MIN_MONSTER_DOLLAR_VOL
      • Alerts once per symbol per day.
    """
    _reset_if_new_day()

    if not _in_volume_window():
        print("[volume] outside RTH window; skipping.")
        return

    if not POLYGON_KEY or not _client:
        print("[volume] missing POLYGON_KEY or client; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[volume] empty universe; skipping.")
        return

    trading_day = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # Compute day stats
        rvol, day_vol, last_price, prev_close, dollar_vol = _compute_rvol_and_day_stats(sym, trading_day)
        if last_price <= 0 or prev_close <= 0:
            continue
        if last_price < MIN_MONSTER_PRICE:
            continue

        # Softer, bot-specific RVOL gate
        if rvol < MIN_VOLUME_RVOL:
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
            continue

        # Intraday minute bars (for actual monster-bar detection)
        bars = _fetch_intraday(sym, trading_day)
        if not bars:
            continue

        found, monster_bar_vol = _find_monster_bar(sym, bars, last_price)
        if not found:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
        grade = grade_equity_setup(move_pct, rvol, dollar_vol)
        bias = "Bullish accumulation" if move_pct >= 0 else "Bearish distribution"

        body = (
            f"💥 Monster Volume Spike Detected\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📦 Biggest 1-min Bar: {int(monster_bar_vol):,} shares "
            f"(≈ ${monster_bar_vol * last_price:,.0f})\n"
            f"📊 RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        time_str = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 VOLUME — {sym}\n"
            f"🕒 {time_str}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)