# bots/swing_pullback.py — Swing Pullback Bot (Uptrend + Clean Dip)

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
    now_est,  # human-readable EST timestamp string
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_PRICE = float(os.getenv("PULLBACK_MAX_PRICE", "200.0"))

# Minimum *daily* dollar volume and RVOL
MIN_DOLLAR_VOL = float(os.getenv("PULLBACK_MIN_DOLLAR_VOL", "30000000"))  # $30M+
MIN_RVOL = float(os.getenv("PULLBACK_MIN_RVOL", "2.0"))

# Pullback % from recent swing high
MIN_PULLBACK_PCT = float(os.getenv("PULLBACK_MIN_PULLBACK_PCT", "3.0"))
MAX_PULLBACK_PCT = float(os.getenv("PULLBACK_MAX_PULLBACK_PCT", "15.0"))

# Max consecutive red days in pullback (1–MAX_RED_DAYS)
MAX_RED_DAYS = int(os.getenv("PULLBACK_MAX_RED_DAYS", "3"))

# How far back to look for trend + swing high
LOOKBACK_DAYS = int(os.getenv("PULLBACK_LOOKBACK_DAYS", "60"))

# ------------------- STATE -------------------

_alert_date: date | None = None
_alerted: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted = set()


def _already(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted


def _mark(sym: str) -> None:
    _reset_if_new_day()
    _alerted.add(sym)


def _in_rth() -> bool:
    """Regular trading hours 09:30–16:00 ET."""
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


def _universe() -> List[str]:
    """TICKER_UNIVERSE env override, else dynamic top-volume universe."""
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.97)


# ------------------- HELPERS -------------------

def _get_close(d) -> float:
    return float(getattr(d, "close", getattr(d, "c", 0.0)) or 0.0)


def _get_volume(d) -> float:
    return float(getattr(d, "volume", getattr(d, "v", 0.0)) or 0.0)


def _sma(values: List[float], window: int) -> List[float]:
    """Simple moving average series (same length as values[window-1:])."""
    if len(values) < window:
        return []
    out: List[float] = []
    for i in range(window - 1, len(values)):
        window_vals = values[i - window + 1 : i + 1]
        out.append(sum(window_vals) / float(window))
    return out


def _compute_rvol(day_vol: float, days: List) -> float:
    """
    Compute RVOL using the previous ~20 trading days (excluding today).
    Falls back to 1.0 if we can't compute properly.
    """
    if len(days) < 2:
        return 1.0
    # Use last 20 days EXCLUDING today
    hist = days[:-1]
    recent = hist[-20:] if len(hist) > 20 else hist
    vols = [_get_volume(d) for d in recent]
    avg_vol = sum(vols) / len(vols) if vols else 0.0
    if avg_vol <= 0:
        return 1.0
    return day_vol / avg_vol


def _count_recent_red_days(closes: List[float]) -> int:
    """
    Count how many of the most recent days are red in a row
    (close < prior close), up to MAX_RED_DAYS.
    """
    if len(closes) < 2:
        return 0
    red_days = 0
    # Walk from most recent backward
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            red_days += 1
            if red_days >= MAX_RED_DAYS:
                break
        else:
            # Stop once we hit a green/flat day
            break
    return red_days


# ------------------- MAIN BOT -------------------

async def run_swing_pullback():
    """
    Swing Pullback Bot — strong uptrend, clean dip into support.

      • Time: RTH only (09:30–16:00 ET)
      • Universe: TICKER_UNIVERSE env OR dynamic top volume universe (200 names)
      • Filters (per symbol):
          - Price between MIN_PRICE and MAX_PRICE
          - Day $ volume ≥ MIN_DOLLAR_VOL (and shares ≥ MIN_VOLUME_GLOBAL)
          - RVOL ≥ max(MIN_RVOL_GLOBAL, MIN_RVOL)
          - Clear uptrend: 20-period SMA > 50-period SMA, price above 50SMA
          - 1–MAX_RED_DAYS consecutive red candles
          - Pullback from recent swing high between MIN_PULLBACK_PCT and MAX_PULLBACK_PCT
    """
    if not POLYGON_KEY or not _client:
        print("[swing_pullback] Missing client/API key.")
        return
    if not _in_rth():
        print("[swing_pullback] Outside RTH; skipping.")
        return

    _reset_if_new_day()
    universe = _universe()
    if not universe:
        print("[swing_pullback] empty universe; skipping.")
        return

    today = date.today()
    today_s = today.isoformat()
    time_str = now_est()  # shared returns a nice EST string

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        # --- Fetch daily data ---
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=LOOKBACK_DAYS + 40)).isoformat(),
                    to=today_s,
                    limit=LOOKBACK_DAYS + 40,
                    sort="asc",
                )
            )
        except Exception as e:
            print(f"[swing_pullback] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 50:
            # Need enough data for 20 & 50 SMA
            continue

        # Focus on most recent LOOKBACK_DAYS window for calculations
        days = days[-LOOKBACK_DAYS:]

        if len(days) < 50:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        last_price = _get_close(today_bar)
        prev_close = _get_close(prev_bar)
        day_vol = _get_volume(today_bar)

        if last_price <= 0 or prev_close <= 0:
            continue

        # Price filter
        if last_price < MIN_PRICE or last_price > MAX_PRICE:
            continue

        # Dollar volume filter (also respect MIN_VOLUME_GLOBAL as shares floor)
        dollar_vol = last_price * day_vol
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_DOLLAR_VOL:
            continue

        # --- RVOL ---
        rvol = _compute_rvol(day_vol, days)
        if rvol < max(MIN_RVOL_GLOBAL, MIN_RVOL):
            continue

        # --- Trend: 20SMA vs 50SMA ---
        closes = [_get_close(d) for d in days]
        sma20_series = _sma(closes, 20)
        sma50_series = _sma(closes, 50)
        if not sma20_series or not sma50_series:
            continue

        sma20 = sma20_series[-1]
        sma50 = sma50_series[-1]

        # Strong uptrend: short MA above long MA, and price above long MA
        if sma20 <= sma50:
            continue
        if last_price <= sma50:
            continue

        # --- Recent pullback (consecutive red days) ---
        # Use the last (MAX_RED_DAYS + 5) closes to get a local view
        recent_window = closes[-(MAX_RED_DAYS + 5) :]
        red_days = _count_recent_red_days(recent_window)
        if red_days == 0 or red_days > MAX_RED_DAYS:
            continue

        # --- Pullback from recent swing high (last ~20 days) ---
        swing_window = closes[-20:]
        swing_high = max(swing_window) if swing_window else 0.0
        if swing_high <= 0:
            continue

        pullback_pct = (swing_high - last_price) / swing_high * 100.0
        if pullback_pct < MIN_PULLBACK_PCT or pullback_pct > MAX_PULLBACK_PCT:
            continue

        # Day move vs yesterday
        move_pct = (last_price / prev_close - 1.0) * 100.0

        grade = grade_equity_setup(move_pct, rvol, dollar_vol)

        # --- Alert formatting (premium style) ---
        extra = (
            f"📈 SWING PULLBACK — {sym}\n"
            f"🕒 {time_str}\n"
            f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"📌 Uptrend: 20 SMA {sma20:.2f} > 50 SMA {sma50:.2f}, price above 50 SMA\n"
            f"📉 Recent pullback: {red_days} red day(s), ~{pullback_pct:.1f}% off swing high\n"
            f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("swing_pullback", sym, last_price, rvol, extra=extra)