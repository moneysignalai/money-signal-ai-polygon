# bots/gap.py — PREMIUM GAP UP / GAP DOWN SCANNER (2025)

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
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# -------------------------- CONFIG --------------------------

MIN_GAP_PRICE = float(os.getenv("MIN_GAP_PRICE", "3.0"))
MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "3.0"))
MIN_PREMARKET_VOL = float(os.getenv("MIN_PREMARKET_VOL", "300000"))  # fewer false positives
MIN_GAP_RVOL = float(os.getenv("MIN_GAP_RVOL", "1.5"))
MIN_GAP_DOLLAR_VOL = float(os.getenv("MIN_GAP_DOLLAR_VOL", "8000000"))
MIN_ATR = float(os.getenv("MIN_ATR", "1.0"))

GAP_SCAN_END_MIN = 11 * 60  # Only scan until 11AM

_alert_date = None
_alerted = set()


# -------------------------- UTILITIES --------------------------

def _reset_if_new_day():
    global _alert_date, _alerted
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted = set()


def _already(sym):
    return sym in _alerted


def _mark(sym):
    _alerted.add(sym)


def _in_gap_window():
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= GAP_SCAN_END_MIN


def _get_universe():
    env = os.getenv("GAP_TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.92)


def _fetch_history(sym: str, days_back=40):
    try:
        start = (date.today() - timedelta(days=days_back)).isoformat()
        return list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                date.today().isoformat(),
                limit=days_back + 5,
                sort="asc"
            )
        )
    except:
        return []


def _calculate_atr(days):
    if len(days) < 15:
        return 0
    trs = []
    for d in days[-15:]:
        high = float(getattr(d, "high", getattr(d, "h", 0)))
        low = float(getattr(d, "low", getattr(d, "l", 0)))
        prev_close = float(getattr(days[-16], "close", getattr(days[-16], "c", 0)))
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs)


# -------------------------- GAP CALC --------------------------

def _compute_gap(sym: str):
    daily = _fetch_history(sym, 40)
    if len(daily) < 3:
        return None

    prev = daily[-2]
    today = daily[-1]

    prev_close = float(prev.close)
    open_today = float(today.open)
    last_price = float(today.close)

    if prev_close <= 0 or open_today <= 0:
        return None

    gap_pct = (open_today - prev_close) / prev_close * 100
    intraday_pct = (last_price - open_today) / open_today * 100
    total_move_pct = (last_price - prev_close) / prev_close * 100

    day_vol = float(today.volume or 0)

    # ATR
    atr = _calculate_atr(daily)

    # RVOL
    hist = daily[:-1]
    recent = hist[-20:]
    avg_vol = sum(float(x.volume) for x in recent) / len(recent) if recent else day_vol
    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

    # Dollar volume
    dollar_vol = day_vol * last_price

    return {
        "prev_close": prev_close,
        "open": open_today,
        "last": last_price,
        "gap_pct": gap_pct,
        "intraday_pct": intraday_pct,
        "total_move_pct": total_move_pct,
        "day_vol": day_vol,
        "rvol": rvol,
        "atr": atr,
        "dollar_vol": dollar_vol
    }


# -------------------------- MAIN BOT --------------------------

async def run_gap():
    _reset_if_new_day()

    if not _in_gap_window():
        print("[gap] window closed")
        return

    if not POLYGON_KEY:
        print("[gap] missing API key")
        return

    universe = _get_universe()
    today = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        stats = _compute_gap(sym)
        if not stats:
            continue

        last = stats["last"]
        if last < MIN_GAP_PRICE:
            continue

        gap_pct = stats["gap_pct"]
        if abs(gap_pct) < MIN_GAP_PCT:
            continue

        if stats["atr"] < MIN_ATR:
            continue

        if stats["rvol"] < max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL):
            continue

        if stats["day_vol"] < MIN_VOLUME_GLOBAL:
            continue

        if stats["dollar_vol"] < MIN_GAP_DOLLAR_VOL:
            continue

        direction = "Gap Up" if gap_pct > 0 else "Gap Down"
        emoji = "🚀" if gap_pct > 0 else "🩸"

        # Bias logic
        if gap_pct > 0:
            if stats["intraday_pct"] > 0:
                bias = "Gap-and-go strength"
            elif stats["intraday_pct"] < 0:
                bias = "Gap fade"
            else:
                bias = "Holding opening gap"
        else:
            if stats["intraday_pct"] < 0:
                bias = "Continuation lower"
            elif stats["intraday_pct"] > 0:
                bias = "Bounce attempt"
            else:
                bias = "Holding downside gap"

        ts = now_est()
        extra = (
            f"📣 GAP — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${stats['last']:.2f} · RVOL {stats['rvol']:.1f}x\n"
            "────────────\n"
            f"{emoji} {direction}: {gap_pct:.1f}%\n"
            f"📈 Prev Close: ${stats['prev_close']:.2f} → Open ${stats['open']:.2f} → Last ${stats['last']:.2f}\n"
            f"📊 Intraday: {stats['intraday_pct']:.1f}% · Total Move: {stats['total_move_pct']:.1f}%\n"
            f"📦 Volume: {int(stats['day_vol']):,}\n"
            f"💵 Dollar Vol: ${stats['dollar_vol']:,.0f}\n"
            f"📏 ATR(15): {stats['atr']:.2f}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("gap", sym, stats["last"], stats["rvol"], extra=extra)