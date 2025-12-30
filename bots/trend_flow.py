# bots/trend_flow.py
#
# Unified trend/swing bot:
#   • Trend Rider (breakouts with strong trend)
#   • Swing Pullback (dip buy in strong uptrend)
#
# Uses daily candles only; runs during RTH but could be limited to 1–2x/day.

import os
import time
from datetime import date, timedelta, datetime
from typing import List, Dict, Any, Optional

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
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
    now_est,
)
from bots.status_report import record_bot_stats

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- GLOBAL CONFIG -------------------

DEFAULT_MAX_UNIVERSE = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
TREND_MAX_UNIVERSE = int(
    os.getenv("TREND_MAX_UNIVERSE", str(DEFAULT_MAX_UNIVERSE))
)


def _trend_universe() -> List[str]:
    # Shared equity universe: allow TREND_TICKER_UNIVERSE override, otherwise use
    # TICKER_UNIVERSE capped by TREND_MAX_UNIVERSE and trimmed to liquid names.
    return resolve_universe_for_bot(
        bot_name="trend_flow",
        bot_env_var="TREND_TICKER_UNIVERSE",
        max_universe_env="TREND_FLOW_MAX_UNIVERSE",
        default_max_universe=TREND_MAX_UNIVERSE,
        apply_dynamic_filters=True,
    )


def _time_str() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def _in_rth() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60  # 09:30–16:00 ET


# ------------------- SWING PULLBACK CONFIG & STATE -------------------

MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_PRICE = float(os.getenv("PULLBACK_MAX_PRICE", "200.0"))
MIN_DOLLAR_VOL = float(os.getenv("PULLBACK_MIN_DOLLAR_VOL", "30000000"))  # $30M+
MIN_RVOL = float(os.getenv("PULLBACK_MIN_RVOL", "2.0"))
MAX_PULLBACK_PCT = float(os.getenv("PULLBACK_MAX_PULLBACK_PCT", "15.0"))
MIN_PULLBACK_PCT = float(os.getenv("PULLBACK_MIN_PULLBACK_PCT", "3.0"))
MAX_RED_DAYS = int(os.getenv("PULLBACK_MAX_RED_DAYS", "3"))
LOOKBACK_DAYS = int(os.getenv("PULLBACK_LOOKBACK_DAYS", "60"))

_pull_date: date | None = None
_pull_alerted: set[str] = set()


def _reset_pull():
    global _pull_date, _pull_alerted
    today = date.today()
    if _pull_date != today:
        _pull_date = today
        _pull_alerted = set()


def _pull_already(sym: str) -> bool:
    return sym in _pull_alerted


def _pull_mark(sym: str):
    _pull_alerted.add(sym)


# ------------------- TREND RIDER CONFIG & STATE -------------------

TREND_MIN_PRICE = float(os.getenv("TREND_MIN_PRICE", "10.0"))
TREND_MAX_PRICE = float(os.getenv("TREND_MAX_PRICE", "500.0"))
TREND_MIN_DOLLAR_VOL = float(os.getenv("TREND_MIN_DOLLAR_VOL", "30000000"))
TREND_MIN_RVOL = float(os.getenv("TREND_MIN_RVOL", "1.5"))
TREND_BREAKOUT_LOOKBACK = int(os.getenv("TREND_BREAKOUT_LOOKBACK", "20"))  # 20-day high/low

_trend_date: date | None = None
_trend_alerted: set[str] = set()


def _reset_trend():
    global _trend_date, _trend_alerted
    today = date.today()
    if _trend_date != today:
        _trend_date = today
        _trend_alerted = set()


def _trend_already(sym: str) -> bool:
    return sym in _trend_alerted


def _trend_mark(sym: str):
    _trend_alerted.add(sym)


# ------------------- HELPERS -------------------

def _sma(values: List[float], window: int) -> List[float]:
    if len(values) < window:
        return []
    out = []
    for i in range(window - 1, len(values)):
        window_vals = values[i - window + 1 : i + 1]
        out.append(sum(window_vals) / float(window))
    return out


# ------------------- SWING PULLBACK CORE -------------------

def _run_swing_pullback_for_symbol(sym: str, days: List[Any]) -> bool:
    """
    Returns True if a Swing Pullback alert fired for this symbol.
    """
    if len(days) < 30:
        return False

    today_bar = days[-1]
    prev_bar = days[-2]

    try:
        last_price = float(today_bar.close)
        prev_close = float(prev_bar.close)
        day_vol = float(today_bar.volume or 0.0)
    except Exception:
        return False

    if last_price < MIN_PRICE or last_price > MAX_PRICE:
        return False

    dollar_vol = last_price * day_vol
    if dollar_vol < max(MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
        return False

    vols = [float(d.volume or 0.0) for d in days[-21:-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return False
    rvol = day_vol / avg_vol
    if rvol < max(MIN_RVOL_GLOBAL, MIN_RVOL):
        return False

    closes = [float(d.close) for d in days]
    sma20_series = _sma(closes, 20)
    sma50_series = _sma(closes, 50)
    if not sma20_series or not sma50_series:
        return False

    sma20 = sma20_series[-1]
    sma50 = sma50_series[-1]

    # Strong uptrend filter
    if sma20 <= sma50:
        return False
    if last_price <= sma50:
        return False

    # Count recent red days
    recent_closes = [float(d.close) for d in days[-(MAX_RED_DAYS + 5) :]]
    red_days = 0
    for i in range(1, len(recent_closes)):
        if recent_closes[i] < recent_closes[i - 1]:
            red_days += 1

    if red_days == 0 or red_days > MAX_RED_DAYS:
        return False

    recent_window = closes[-20:]
    swing_high = max(recent_window)
    if swing_high <= 0:
        return False

    pullback_pct = (swing_high - last_price) / swing_high * 100.0
    if pullback_pct < MIN_PULLBACK_PCT or pullback_pct > MAX_PULLBACK_PCT:
        return False

    move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    timestamp = _time_str()
    extra = (
        f"📈 SWING PULLBACK — {sym}\n"
        f"🕒 {timestamp}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"📌 Strong uptrend: 20 SMA {sma20:.2f} > 50 SMA {sma50:.2f}\n"
        f"📉 Recent pullback: {red_days} red days, ~{pullback_pct:.1f}% from high\n"
        f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
        f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
        f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    _pull_mark(sym)
    send_alert("swing_pullback", sym, last_price, rvol, extra=extra)
    return True


# ------------------- TREND RIDER CORE -------------------

def _run_trend_rider_for_symbol(sym: str, days: List[Any]) -> bool:
    """
    Returns True if a Trend Rider alert fired for this symbol.
    """
    if len(days) < 60:
        return False

    today_bar = days[-1]
    prev_bar = days[-2]

    try:
        last_price = float(today_bar.close)
        prev_close = float(prev_bar.close)
        day_vol = float(today_bar.volume or 0.0)
        day_high = float(today_bar.high or 0.0)
        day_low = float(today_bar.low or 0.0)
    except Exception:
        return False

    if last_price < TREND_MIN_PRICE or last_price > TREND_MAX_PRICE:
        return False

    dollar_vol = last_price * day_vol
    if dollar_vol < max(TREND_MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
        return False

    vols = [float(d.volume or 0.0) for d in days[-21:-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return False
    rvol = day_vol / avg_vol
    if rvol < max(TREND_MIN_RVOL, MIN_RVOL_GLOBAL):
        return False

    closes = [float(d.close) for d in days]
    sma20_series = _sma(closes, 20)
    sma50_series = _sma(closes, 50)
    if not sma20_series or not sma50_series:
        return False

    sma20 = sma20_series[-1]
    sma50 = sma50_series[-1]
    if sma20 <= sma50:
        return False

    lookback = closes[-TREND_BREAKOUT_LOOKBACK - 1 : -1]
    if not lookback:
        return False
    prior_high = max(lookback)
    prior_low = min(lookback)

    breakout_up = last_price > prior_high
    breakout_down = last_price < prior_low

    if not (breakout_up or breakout_down):
        return False

    move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    direction = "UPTREND BREAKOUT" if breakout_up else "DOWNTREND BREAKDOWN"
    emoji = "🚀" if breakout_up else "⚠️"

    timestamp = _time_str()
    body = (
        f"{emoji} {direction}\n"
        f"📈 20 SMA {sma20:.2f} > 50 SMA {sma50:.2f}\n"
        f"📏 Prior {TREND_BREAKOUT_LOOKBACK}-day range: {prior_low:.2f} – {prior_high:.2f}\n"
        f"📍 Today: Low {day_low:.2f} – High {day_high:.2f} – Close {last_price:.2f}\n"
        f"📊 Day Move: {move_pct:.1f}% · RVOL {rvol:.1f}x\n"
        f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 TREND RIDER — {sym}\n"
        f"🕒 {timestamp}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _trend_mark(sym)
    send_alert("trend_rider", sym, last_price, rvol, extra=extra)
    return True


# ------------------- MAIN ENTRYPOINT -------------------

async def run_trend_flow():
    """
    Unified trend/swing engine:

      • Trend Rider: breakout with strong trend
      • Swing Pullback: dip in strong uptrend
      • Universe: TREND_TICKER_UNIVERSE env OR dynamic top volume universe
      • Runs during RTH; can be tuned by SCAN_INTERVAL_SECONDS in main.py.
    """
    if not POLYGON_KEY or not _client:
        print("[trend_flow] Missing client/API key.")
        return
    if not _in_rth():
        print("[trend_flow] Outside RTH; skipping.")
        return

    BOT_NAME = "trend_flow"
    start_ts = time.time()
    alerts_sent = 0
    matched_symbols: set[str] = set()

    _reset_pull()
    _reset_trend()

    universe = _trend_universe()
    if not universe:
        print("[trend_flow] empty universe; skipping.")
        return

    today = date.today()
    start = (today - timedelta(days=LOOKBACK_DAYS + 50)).isoformat()
    end = today.isoformat()

    print(f"[trend_flow] scanning {len(universe)} symbols at {_time_str()}")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=start,
                    to=end,
                    limit=LOOKBACK_DAYS + 60,
                )
            )
        except Exception as e:
            print(f"[trend_flow] daily fetch failed for {sym}: {e}")
            continue

        if not days:
            continue

        fired_pull = False
        fired_trend = False

        # Swing Pullback
        if not _pull_already(sym):
            fired_pull = _run_swing_pullback_for_symbol(sym, days)
            if fired_pull:
                matched_symbols.add(sym)
                alerts_sent += 1

        # Trend Rider
        if not _trend_already(sym):
            fired_trend = _run_trend_rider_for_symbol(sym, days)
            if fired_trend:
                matched_symbols.add(sym)
                alerts_sent += 1

    run_seconds = time.time() - start_ts

    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matched_symbols),
            alerts=alerts_sent,
            runtime=run_seconds,
        )
    except Exception as e:
        print(f"[trend_flow] record_bot_stats error: {e}")

    print(
        f"[trend_flow] scan complete: scanned={len(universe)} "
        f"matches={len(matched_symbols)} alerts={alerts_sent} "
        f"runtime={run_seconds:.2f}s"
    )