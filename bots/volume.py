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

MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "8000000"))
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "30000000"))
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))


def _in_volume_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60  # RTH


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


async def run_volume():
    """
    Volume Monster Bot:

      • Scans for single 1-min bars with huge volume (MIN_MONSTER_BAR_SHARES, MIN_MONSTER_DOLLAR_VOL).
      • Only during RTH.
      • Requires underlying meet RVOL, volume, price filters.
    """
    if not POLYGON_KEY or not _client:
        print("[volume] no API key/client; skipping.")
        return
    if not _in_volume_window():
        print("[volume] outside RTH; skipping.")
        return

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # daily for RVOL / prev close
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
            print(f"[volume] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_day = days[-1]
        prev_day = days[-2]

        prev_close = float(prev_day.close)
        last_price = float(today_day.close)
        if last_price < MIN_MONSTER_PRICE or prev_close <= 0:
            continue

        day_vol = float(today_day.volume)
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = day_vol

        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
        if rvol < max(2.0, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0

        # minute bars – look for the monster
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
            print(f"[volume] minute fetch failed for {sym}: {e}")
            continue

        if not mins:
            continue

        monster = max(mins, key=lambda m: float(m.volume or 0.0))
        monster_vol = float(monster.volume or 0.0)
        monster_price = float(monster.close or 0.0)

        if monster_vol < MIN_MONSTER_BAR_SHARES:
            continue

        bias = "Aggressive buying on the tape" if monster_price >= last_price else "Aggressive selling on the tape"

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        body = (
            f"📊 Monster 1-min bar: {int(monster_vol):,} shares\n"
            f"💹 Bar Close: ${monster_price:.2f}\n"
            f"📈 Prev Close: ${prev_close:.2f} → Day Close: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📊 RVOL: {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 VOLUME — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)