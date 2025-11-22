# bots/premarket.py
#
# Premarket gap / momentum bot (stocks only)
#
# Looks for:
#   • Meaningful premarket % move vs prior close
#   • Real premarket liquidity (shares + $ notional)
#   • Decent partial-day RVOL vs last 20 sessions
#   • Avoids ETFs + de-dupes per symbol per day

import os
import time
from datetime import date, timedelta, datetime
from typing import List, Tuple, Optional, Any

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
    chart_link,
    grade_equity_setup,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

MIN_PREMARKET_PRICE       = float(os.getenv("MIN_PREMARKET_PRICE", "5.0"))
MIN_PREMARKET_MOVE_PCT    = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "3.0"))
MIN_PREMARKET_DOLLAR_VOL  = float(os.getenv("MIN_PREMARKET_DOLLAR_VOL", "500000"))   # $500k+
MIN_PREMARKET_RVOL        = float(os.getenv("MIN_PREMARKET_RVOL", "1.5"))

# Optional cap on insane micro-cap runners (0 = disabled)
MAX_PREMARKET_MOVE_PCT    = float(os.getenv("MAX_PREMARKET_MOVE_PCT", "0.0"))

# Premarket window (EST)
PREMARKET_START_MIN       = 4 * 60        # 04:00
PREMARKET_END_MIN         = 9 * 60 + 29   # 09:29

# Universe
PREMARKET_MAX_UNIVERSE    = int(os.getenv("PREMARKET_MAX_UNIVERSE", "120"))

# ---------------- STATE ----------------

_alert_date: Optional[date] = None
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


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _alerted.add(sym)


def _in_premarket_window() -> bool:
    """Only run 04:00–09:29 ET (premarket)."""
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return PREMARKET_START_MIN <= mins <= PREMARKET_END_MIN


# ---------------- HELPERS ----------------

def _safe_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _get_universe() -> List[str]:
    """
    Universe priority:
      1) PREMARKET_TICKER_UNIVERSE (if set)
      2) TICKER_UNIVERSE (global override)
      3) Dynamic top volume universe
    """
    env = os.getenv("PREMARKET_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=PREMARKET_MAX_UNIVERSE, volume_coverage=0.90)


def _fetch_daily_history(sym: str, trading_day: date, lookback_days: int = 40) -> List[Any]:
    """Fetch up to lookback_days daily candles up to and including trading_day."""
    if not _client:
        return []
    try:
        start = (trading_day - timedelta(days=lookback_days)).isoformat()
        end = trading_day.isoformat()
        days = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                limit=lookback_days,
                sort="asc",
            )
        )
        return days
    except Exception as e:
        print(f"[premarket] daily fetch failed for {sym}: {e}")
        return []


def _get_prev_and_today(sym: str, trading_day: date) -> Tuple[Optional[Any], Optional[Any], List[Any]]:
    """
    Return (prev_day_bar, today_bar, days_history).
    days_history includes both prev and today bars.
    """
    days = _fetch_daily_history(sym, trading_day, lookback_days=40)
    if len(days) < 2:
        return None, None, days
    return days[-2], days[-1], days


def _get_bar_timestamp_et(bar: Any) -> Optional[datetime]:
    """
    Convert Polygon agg bar timestamp (ms or s) to a timezone-aware datetime in EST.
    """
    ts = getattr(bar, "timestamp", None)
    if ts is None:
        ts = getattr(bar, "t", None)
    if ts is None:
        return None
    try:
        # Heuristic: if it's huge, assume ms
        if ts > 1e12:
            ts = ts / 1000.0
        dt_utc = datetime.fromtimestamp(ts, tz=pytz.UTC)
        return dt_utc.astimezone(eastern)
    except Exception:
        return None


def _get_premarket_window_aggs(sym: str, trading_day: date) -> Tuple[float, float, float, float]:
    """
    Return (pre_low, pre_high, last_px, pre_vol) for 04:00–09:29 ET on trading_day.
    """
    if not _client:
        return 0.0, 0.0, 0.0, 0.0

    try:
        bars = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="minute",
                from_=(trading_day - timedelta(days=1)).isoformat(),
                to=trading_day.isoformat(),
                limit=2000,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[premarket] minute fetch failed for {sym}: {e}")
        return 0.0, 0.0, 0.0, 0.0

    if not bars:
        return 0.0, 0.0, 0.0, 0.0

    pre_lows: List[float] = []
    pre_highs: List[float] = []
    pre_vols: List[float] = []
    pre_last_px: float = 0.0

    for b in bars:
        dt_et = _get_bar_timestamp_et(b)
        if not dt_et or dt_et.date() != trading_day:
            continue
        mins = dt_et.hour * 60 + dt_et.minute
        if PREMARKET_START_MIN <= mins <= PREMARKET_END_MIN:
            low = _safe_float(getattr(b, "low", getattr(b, "l", None)))
            high = _safe_float(getattr(b, "high", getattr(b, "h", None)))
            vol = _safe_float(getattr(b, "volume", getattr(b, "v", None)))
            close = _safe_float(getattr(b, "close", getattr(b, "c", None)))

            if low == 0.0 and high == 0.0 and close == 0.0:
                continue

            pre_lows.append(low if low > 0 else close)
            pre_highs.append(high if high > 0 else close)
            pre_vols.append(vol)
            pre_last_px = close

    if not pre_lows or pre_last_px <= 0:
        return 0.0, 0.0, 0.0, 0.0

    pre_low = min(pre_lows)
    pre_high = max(pre_highs)
    pre_vol = sum(pre_vols)

    return pre_low, pre_high, pre_last_px, pre_vol


def _compute_partial_rvol(sym: str, trading_day: date, today_bar: Any, days: List[Any]) -> float:
    """
    Compute partial-day RVOL:
      RVOL_partial = today_partial_vol / avg(20d historical vol)
    """
    todays_partial_vol = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))

    hist = days[:-1]
    if hist:
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = (
            sum(_safe_float(getattr(d, "volume", getattr(d, "v", None))) for d in recent)
            / float(len(recent))
        )
    else:
        avg_vol = todays_partial_vol

    if avg_vol > 0:
        return todays_partial_vol / avg_vol

    return 1.0

        #------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
from bots.status_report import record_bot_stats

BOT_NAME = "premarket"
...
start_ts = time.time()
alerts_sent = 0
matches = []

# ... your scan logic ...

run_seconds = time.time() - start_ts

record_bot_stats(
    BOT_NAME,
    scanned=len(universe),
    matched=len(matches),
    alerts=alerts_sent,
    runtime=run_seconds,
)

# ---------------- MAIN BOT ----------------

async def run_premarket() -> None:
    """
    Premarket gap / momentum bot.

    Filters:
      - Price >= MIN_PREMARKET_PRICE
      - |Move vs prior close| >= MIN_PREMARKET_MOVE_PCT
      - If MAX_PREMARKET_MOVE_PCT > 0 → also cap absurd micro-cap moves
      - Premarket dollar volume >= MIN_PREMARKET_DOLLAR_VOL
      - Partial-day RVOL >= max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL)
      - Day volume so far >= MIN_VOLUME_GLOBAL
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[premarket] POLYGON_KEY or client missing; skipping.")
        return

    if not _in_premarket_window():
        print("[premarket] Outside premarket window; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[premarket] empty universe; skipping.")
        return

    trading_day = date.today()
    today_s = trading_day.isoformat()
    print(f"[premarket] scanning {len(universe)} symbols for premarket movers ({today_s})")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        prev_bar, today_bar, days = _get_prev_and_today(sym, trading_day)
        if not prev_bar or not today_bar:
            continue

        # Previous close
        prev_close = _safe_float(getattr(prev_bar, "close", getattr(prev_bar, "c", None)))
        if prev_close <= 0:
            continue

        # Partial day volume (includes premarket)
        todays_partial_vol = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))

        # Premarket minute bars
        pre_low, pre_high, last_px, pre_vol = _get_premarket_window_aggs(sym, trading_day)
        if last_px <= 0 or pre_vol <= 0:
            continue

        if last_px < MIN_PREMARKET_PRICE:
            continue

        move_pct = (last_px - prev_close) / prev_close * 100.0
        abs_move = abs(move_pct)
        if abs_move < MIN_PREMARKET_MOVE_PCT:
            continue
        if MAX_PREMARKET_MOVE_PCT > 0.0 and abs_move > MAX_PREMARKET_MOVE_PCT:
            # Optional: skip insane 150–300% premarket pumps if you want cleaner feed
            continue

        pre_dollar_vol = last_px * pre_vol
        if pre_dollar_vol < MIN_PREMARKET_DOLLAR_VOL:
            continue

        # Partial RVOL
        rvol = _compute_partial_rvol(sym, trading_day, today_bar, days)
        if rvol < max(MIN_PREMARKET_RVOL, MIN_RVOL_GLOBAL):
            continue

        # Make sure partial day volume is not tiny
        if todays_partial_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol_day_partial = last_px * todays_partial_vol

        # Grade uses magnitude of move, RVOL and partial day $ volume
        grade = grade_equity_setup(abs_move, rvol, dollar_vol_day_partial)

        direction = "up" if move_pct > 0 else "down"
        emoji = "🚀" if move_pct > 0 else "⚠️"
        bias = (
            "Long premarket momentum / gap-and-go watch"
            if move_pct > 0
            else "Gap-down pressure; watch for flush or bounce"
        )

        body = (
            f"{emoji} Premarket move: {move_pct:.1f}% {direction} vs prior close\n"
            f"📈 Prev Close: ${prev_close:.2f} → Premarket Last: ${last_px:.2f}\n"
            f"📊 Premarket Range: ${pre_low:.2f} – ${pre_high:.2f}\n"
            f"📦 Premarket Vol: {pre_vol:,.0f} (≈ ${pre_dollar_vol:,.0f})\n"
            f"💰 Day Vol (partial): {todays_partial_vol:,.0f} (≈ ${dollar_vol_day_partial:,.0f})\n"
            f"📊 RVOL (partial): {rvol:.1f}x\n"
            f"🎯 Grade: {grade}\n"
            f"🧠 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)

        extra = (
            f"📣 PREMARKET — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_px:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("premarket", sym, last_px, rvol, extra=extra)

    print("[premarket] scan complete.")
