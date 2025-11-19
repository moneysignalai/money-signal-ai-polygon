import os
from datetime import date, timedelta, datetime
from typing import List, Tuple

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
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ---- Config / thresholds ----

MIN_PREMARKET_PRICE = float(os.getenv("MIN_PREMARKET_PRICE", "2.0"))
MIN_PREMARKET_MOVE_PCT = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "8.0"))      # vs prior close
MIN_PREMARKET_RVOL = float(os.getenv("MIN_PREMARKET_RVOL", "2.0"))              # RVOL floor
MIN_PREMARKET_DOLLAR_VOL = float(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "3000000"))  # $3M+

# ---- Per-day de-dupe ----

_premarket_alert_date: date | None = None
_premarket_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _premarket_alert_date, _premarket_alerted_syms
    today = date.today()
    if _premarket_alert_date != today:
        _premarket_alert_date = today
        _premarket_alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _premarket_alerted_syms


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _premarket_alerted_syms.add(sym)


# ---- Time & universe helpers ----

def _in_premarket_window() -> bool:
    """
    Premarket session: 4:00–9:29 AM EST.
    """
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 4 * 60 <= minutes < 9 * 60 + 30


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _filter_premarket_minutes(mins) -> List:
    """
    Keep only 1-min bars between 4:00 and 9:29 AM EST.
    """
    out = []
    for b in mins:
        # Polygon timestamps are usually in ms
        ts = getattr(b, "timestamp", None)
        if ts is None:
            continue
        dt_utc = datetime.utcfromtimestamp(ts / 1000.0).replace(tzinfo=pytz.UTC)
        dt_et = dt_utc.astimezone(eastern)
        minutes = dt_et.hour * 60 + dt_et.minute
        if 4 * 60 <= minutes < 9 * 60 + 30:
            out.append(b)
    return out


def _premarket_stats(mins, prev_close: float) -> Tuple[float, float, float, float]:
    """
    Return (last_px, pre_high, pre_low, pre_move_pct) using the premarket bars.
    """
    if not mins:
        return 0.0, 0.0, 0.0, 0.0

    pre_high = max(float(b.high) for b in mins)
    pre_low = min(float(b.low) for b in mins)
    last_px = float(mins[-1].close)

    if prev_close > 0:
        move_pct = (last_px - prev_close) / prev_close * 100.0
    else:
        move_pct = 0.0

    return last_px, pre_high, pre_low, move_pct


# ---- Main bot ----

async def run_premarket():
    """
    Premarket Runner:

      • +MIN_PREMARKET_MOVE_PCT% or more vs yesterday close
      • Price >= MIN_PREMARKET_PRICE
      • Day RVOL >= max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL)
      • Day volume >= MIN_VOLUME_GLOBAL
      • Premarket dollar volume >= MIN_PREMARKET_DOLLAR_VOL
      • Only between 4:00–9:29 AM EST
      • Each symbol alerts at most once per day
    """
    if not POLYGON_KEY:
        print("[premarket] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[premarket] Client not initialized; skipping scan.")
        return
    if not _in_premarket_window():
        print("[premarket] Outside 4:00–9:29 window; skipping scan.")
        return

    _reset_if_new_day()

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # --- Daily bars for prev close + RVOL baseline ---
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
            print(f"[premarket] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        prev_close = float(prev_bar.close)
        if prev_close <= 0:
            continue

        # RVOL based on partial day volume vs historical full days
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        todays_partial_vol = float(today_bar.volume)
        if avg_vol > 0:
            rvol = todays_partial_vol / avg_vol
        else:
            rvol = 1.0

        if rvol < max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL):
            continue

        if todays_partial_vol < MIN_VOLUME_GLOBAL:
            continue

        # --- Minute bars for true premarket move/volume ---
        try:
            mins_all = list(
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
            print(f"[premarket] minute fetch failed for {sym}: {e}")
            continue

        premins = _filter_premarket_minutes(mins_all)
        if not premins:
            continue

        last_px, pre_high, pre_low, move_pct = _premarket_stats(premins, prev_close)
        if last_px < MIN_PREMARKET_PRICE:
            continue

        if abs(move_pct) < MIN_PREMARKET_MOVE_PCT:
            continue

        pre_vol = float(sum(b.volume for b in premins))
        pre_dollar_vol = last_px * pre_vol
        if pre_dollar_vol < MIN_PREMARKET_DOLLAR_VOL:
            continue

        dv = last_px * todays_partial_vol
        grade = grade_equity_setup(abs(move_pct), rvol, dv)

        direction = "up" if move_pct > 0 else "down"
        emoji = "🚀" if move_pct > 0 else "⚠️"
        bias = (
            "Long premarket momentum"
            if move_pct > 0
            else "Watch for continuation / short setup on weakness"
        )

        extra = (
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
        send_alert("premarket", sym, last_px, rvol, extra=extra)