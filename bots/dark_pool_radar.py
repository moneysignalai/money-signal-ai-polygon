# bots/dark_pool_radar.py — OPTIMIZED / MORE ALERTS / POLYGON-SAFE

import os
import time
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
    resolve_universe_for_bot,
    chart_link,
    is_etf_blacklisted,
)

from bots.status_report import record_bot_stats

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

# Exchanges considered "dark" / ATS-like
DARK_EXCHANGES = {
    8, 9, 80, 81, 82, 84, 87, 88, 201, 202
}

# Lookback window (minutes) for dark pool prints
DARK_LOOKBACK_MIN = int(os.getenv("DARK_LOOKBACK_MIN", "45"))

# Notional / count thresholds
MIN_DARK_TOTAL_NOTIONAL = float(os.getenv("DARK_MIN_TOTAL_NOTIONAL", "1000000"))
MIN_DARK_SINGLE_NOTIONAL = float(os.getenv("DARK_MIN_SINGLE_NOTIONAL", "500000"))
MIN_DARK_PRINT_COUNT = int(os.getenv("DARK_MIN_PRINT_COUNT", "1"))

# Looser day filters for more names
MIN_DARK_DOLLAR_VOL = float(os.getenv("DARK_MIN_DOLLAR_VOL", "10000000"))
MIN_DARK_RVOL = float(os.getenv("DARK_MIN_RVOL", "1.0"))

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
    return 4 * 60 <= mins <= 20 * 60 + 15


def _universe() -> List[str]:
    # Universe driven by TICKER_UNIVERSE unless DARK_POOL_MAX_UNIVERSE caps it; apply
    # dynamic trimming so counts stay aligned with other equity bots.
    default_max = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
    return resolve_universe_for_bot(
        bot_name="dark_pool_radar",
        bot_env_var="DARK_POOL_TICKER_UNIVERSE",
        max_universe_env="DARK_POOL_MAX_UNIVERSE",
        default_max_universe=default_max,
        apply_dynamic_filters=True,
    )


def _safe(o: Any, name: str, default=None):
    try:
        return getattr(o, name, default)
    except Exception:
        return default


# ------------------- MAIN BOT -------------------

async def run_dark_pool_radar():
    if not POLYGON_KEY or not _client:
        print("[dark_pool] Missing client/API key.")
        return
    if not _in_dark_window():
        print("[dark_pool] Outside dark-pool window; skipping.")
        return

    BOT_NAME = "dark_pool_radar"
    scan_start_ts = time.time()

    _reset_if_new_day()
    universe = _universe()

    matches: list[str] = []
    alerts_sent = 0

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

        # ---- Fetch daily bars ----
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=60)).isoformat(),
                    to=today_s,
                    limit=60,
                )
            )
        except Exception as e:
            print(f"[dark_pool] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 5:
            continue

        try:
            today_bar = days[-1]
            prev_bar = days[-2]
            last_price = float(getattr(today_bar, "close", getattr(today_bar, "c", 0.0)))
            prev_close = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0.0)))
            day_vol = float(getattr(today_bar, "volume", getattr(today_bar, "v", 0.0)))
        except Exception:
            continue

        if last_price <= 0 or day_vol <= 0:
            continue

        # ---- Dollar volume filter ----
        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_DARK_DOLLAR_VOL:
            continue

        # ---- RVOL filter ----
        vols = [float(getattr(d, "volume", getattr(d, "v", 0.0))) for d in days[-21:-1]]
        avg_vol = sum(vols) / max(len(vols), 1)
        if avg_vol <= 0:
            continue
        rvol = day_vol / avg_vol
        if rvol < MIN_DARK_RVOL:
            continue

        move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0

        # ---- Fetch recent trades ----
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
            print(f"[dark_pool] trades fetch failed for {sym}: {e}")
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

        # ---- Alert Triggers ----
        if trade_count < MIN_DARK_PRINT_COUNT:
            continue

        if (
            total_dark_notional < MIN_DARK_TOTAL_NOTIONAL
            and largest_dark_print < MIN_DARK_SINGLE_NOTIONAL
        ):
            continue

        # At this point, sym is a "match"
        matches.append(sym)
        alerts_sent += 1

        now_str = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        # Body only – header (emoji + DARK POOL — TICKER) comes from send_alert
        extra = (
            f"🕒 {now_str}\n"
            f"📡 Dark pool prints (last {DARK_LOOKBACK_MIN} min)\n"
            f"────────────\n"
            f"📦 Prints: {trade_count:,}\n"
            f"💰 Total Notional: ≈ ${total_dark_notional:,.0f}\n"
            f"🏦 Largest Print: ≈ ${largest_dark_print:,.0f}\n"
            f"📊 Day Move: {move_pct:.1f}% · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("dark_pool", sym, last_price, rvol, extra=extra)

    # ---------------- STATS REPORTING ----------------
    run_seconds = time.time() - scan_start_ts

    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matches),
            alerts=alerts_sent,
            runtime_seconds=run_seconds,
        )
    except Exception as e:
        print(f"[dark_pool] record_bot_stats error: {e}")

    print(
        f"[dark_pool] scan complete: scanned={len(universe)} "
        f"matches={len(matches)} alerts={alerts_sent} "
        f"runtime={run_seconds:.2f}s"
    )