# bots/dark_pool_radar.py

import os
from datetime import date, timedelta, datetime
from typing import List, Any

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
    chart_link,
    is_etf_blacklisted,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

# Exchange codes to be treated as dark/ATS — tune as needed
DARK_EXCHANGES = {
    8, 9, 80, 81, 82  # placeholders; adapt to your feed
}

DARK_LOOKBACK_MIN = int(os.getenv("DARK_LOOKBACK_MIN", "30"))  # last X minutes
MIN_DARK_TOTAL_NOTIONAL = float(os.getenv("DARK_MIN_TOTAL_NOTIONAL", "10000000"))  # $10M+
MIN_DARK_SINGLE_NOTIONAL = float(os.getenv("DARK_MIN_SINGLE_NOTIONAL", "5000000"))  # $5M+
MIN_DARK_PRINT_COUNT = int(os.getenv("DARK_MIN_PRINT_COUNT", "3"))
MIN_DARK_DOLLAR_VOL = float(os.getenv("DARK_MIN_DOLLAR_VOL", "25000000"))  # $25M+
MIN_DARK_RVOL = float(os.getenv("DARK_MIN_RVOL", "1.5"))

# ------------------- STATE -------------------

_alert_date: date | None = None
_alerted: set[str] = set()


def _reset_if_new_day():
    global _alert_date, _alerted
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted = set()


def _already(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted


def _mark(sym: str):
    _reset_if_new_day()
    _alerted.add(sym)


def _in_dark_window() -> bool:
    """
    Dark pool radar runs from 04:00 to 20:15 ET.
    """
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 4 * 60 <= mins <= 20 * 60 + 15  # 4:00–20:15 ET


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


def _safe(o: Any, name: str, default=None):
    return getattr(o, name, default)


# ------------------- MAIN BOT -------------------


async def run_dark_pool_radar():
    """
    Dark Pool Radar Bot — "oh wow" clusters.

      • Time: 04:00–20:15 EST
      • Universe: TICKER_UNIVERSE env OR dynamic top volume universe (120 names)
      • Filters:
          - Day $ volume ≥ MIN_DARK_DOLLAR_VOL
          - RVOL ≥ max(MIN_RVOL_GLOBAL, MIN_DARK_RVOL)
          - Dark prints (last DARK_LOOKBACK_MIN minutes):
              - Total dark notional ≥ MIN_DARK_TOTAL_NOTIONAL  OR
              - Largest single dark print ≥ MIN_DARK_SINGLE_NOTIONAL
              - At least MIN_DARK_PRINT_COUNT prints
    """
    if not POLYGON_KEY or not _client:
        print("[dark_pool] Missing client/API key.")
        return
    if not _in_dark_window():
        print("[dark_pool] Outside 04:00–20:15 EST window; skipping.")
        return

    _reset_if_new_day()
    universe = _universe()

    now_et = datetime.now(eastern)
    end_ts = now_et
    start_ts = now_et - timedelta(minutes=DARK_LOOKBACK_MIN)

    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        # --- Daily context ---
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
            print(f"[dark_pool] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 10:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        try:
            last_price = float(today_bar.close)
            prev_close = float(prev_bar.close)
            day_vol = float(today_bar.volume or 0.0)
        except Exception:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < max(MIN_DARK_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
            continue

        vols = [float(d.volume or 0.0) for d in days[-11:-1]]
        avg_vol = sum(vols) / max(len(vols), 1)
        if avg_vol <= 0:
            continue
        rvol = day_vol / avg_vol
        if rvol < max(MIN_RVOL_GLOBAL, MIN_DARK_RVOL):
            continue

        move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0

        # --- Dark trades over last DARK_LOOKBACK_MIN minutes ---
        total_dark_notional = 0.0
        largest_dark_print = 0.0
        trade_count = 0

        try:
            trades = _client.list_trades(
                ticker=sym,
                timestamp_gte=int(start_ts.timestamp() * 1_000_000_000),
                timestamp_lte=int(end_ts.timestamp() * 1_000_000_000),
                limit=5000,
            )
        except Exception as e:
            print(f"[dark_pool] trade fetch failed for {sym}: {e}")
            continue

        for t in trades:
            ex = _safe(t, "exchange", None)
            if ex not in DARK_EXCHANGES:
                continue

            price = float(_safe(t, "price", 0.0) or 0.0)
            size = float(_safe(t, "size", 0.0) or 0.0)
            if price <= 0 or size <= 0:
                continue

            notional = price * size
            total_dark_notional += notional
            trade_count += 1
            if notional > largest_dark_print:
                largest_dark_print = notional

        if trade_count < MIN_DARK_PRINT_COUNT:
            continue

        if (
            total_dark_notional < MIN_DARK_TOTAL_NOTIONAL
            and largest_dark_print < MIN_DARK_SINGLE_NOTIONAL
        ):
            continue

        # --- Alert ---
        now_str = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")
        emoji = "📡"
        radar_emoji = "📡"
        money_emoji = "💰"
        divider = "────────────"

        extra = (
            f"{emoji} DARK POOL RADAR — {sym}\n"
            f"🕒 {now_str}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{radar_emoji} Dark pool cluster (last {DARK_LOOKBACK_MIN} min)\n"
            f"📦 Prints: {trade_count:,}\n"
            f"💰 Total Dark Notional: ≈ ${total_dark_notional:,.0f}\n"
            f"🏦 Largest Single Print: ≈ ${largest_dark_print:,.0f}\n"
            f"📊 Day Move: {move_pct:.1f}% · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("dark_pool", sym, last_price, rvol, extra=extra)
