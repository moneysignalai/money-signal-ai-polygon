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
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
    now_est,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------- CONFIG -------
# Loosened so you actually see more "monster" volume names:
MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "1000000"))   # 1M+
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "10000000")) # $10M+
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "3.0"))

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


# ------- MAIN BOT -------
async def run_volume():
    """
    Volume Monster Bot (FIXED):

      • Uses correct Polygon minute agg timestamps (Unix ms).
      • Scans for 1-minute volume spikes.
      • RVOL, price, and dollar-volume filters.
      • Premium alert formatting.
    """

    if not POLYGON_KEY or not _client:
        print("[volume] no API key; skipping.")
        return

    if not _in_volume_window():
        print("[volume] outside RTH; skipping.")
        return

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    # Compute start-of-day and now timestamps for minute bars
    now_et = datetime.now(eastern)
    sod = datetime(now_et.year, now_et.month, now_et.day, 9, 30, 0, tzinfo=eastern)

    sod_ms = int(sod.timestamp() * 1000)
    now_ms = int(now_et.timestamp() * 1000)

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # 1) Day-level filters via daily aggs
        try:
            daily = _client.list_aggs(
                sym,
                1,
                "day",
                (today - timedelta(days=20)).isoformat(),
                today_s,
                limit=30,
                sort="asc",
            )
            days = list(daily)
        except Exception as e:
            print(f"[volume] daily aggs error for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        d0 = days[-1]   # today
        d1 = days[-2]   # prior day

        last_price = float(getattr(d0, "close", getattr(d0, "c", 0.0)))
        prev_close = float(getattr(d1, "close", getattr(d1, "c", 0.0)))

        if last_price < MIN_MONSTER_PRICE or prev_close <= 0:
            continue

        # RVOL
        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0.0))) for d in recent) / len(recent)
        day_vol = float(getattr(d0, "volume", getattr(d0, "v", 0.0)))
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        # Loosened RVOL threshold a bit
        if rvol < max(1.8, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0

        # 2) Minute-level monster bar
        try:
            mins_iter = _client.list_aggs(
                sym,
                1,
                "minute",
                today_s,
                today_s,
                limit=1500,
                sort="asc",
            )
            mins = [m for m in mins_iter if getattr(m, "timestamp", getattr(m, "t", None)) and sod_ms <= getattr(m, "timestamp", getattr(m, "t", 0)) <= now_ms]
        except Exception as e:
            print(f"[volume] minute aggs error for {sym}: {e}")
            continue

        if not mins:
            continue

        # find highest volume bar
        def _vol(m):
            return float(getattr(m, "volume", getattr(m, "v", 0.0)) or 0.0)

        monster = max(mins, key=_vol)
        monster_vol = _vol(monster)
        monster_price = float(getattr(monster, "close", getattr(monster, "c", last_price)))

        if monster_vol < MIN_MONSTER_BAR_SHARES:
            continue

        # Bias
        bias = (
            "Aggressive buying detected"
            if monster_price >= last_price
            else "Aggressive selling detected"
        )

        # Grade
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        # -------------------------------------------------------------
        # PREMIUM ALERT FORMAT (MATCHED TO ALL OTHER BOTS)
        # -------------------------------------------------------------
        body = (
            f"📊 Monster 1-min bar: {monster_vol:,.0f} shares\n"
            f"💹 Bar Close: ${monster_price:.2f}\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        ts = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"📣 VOLUME — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)