"""
Squeeze bot
------------
Price + volume-only short-squeeze radar. With no direct short-interest feed, it
flags names that are screaming on the tape: large up moves, heavy relative
volume, strong drive from the open, and closes near the high of day.
"""

from collections import defaultdict
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    POLYGON_KEY,
    chart_link,
    debug_filter_reason,
    in_rth_window_est,
    is_etf_blacklisted,
    now_est,
    resolve_universe_for_bot,
    send_alert,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

BOT_NAME = "Squeeze"

SQUEEZE_MAX_UNIVERSE = int(os.getenv("SQUEEZE_MAX_UNIVERSE", "2000"))
SQUEEZE_ALLOW_OUTSIDE_RTH = (
    os.getenv("SQUEEZE_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
)
SQUEEZE_LOOKBACK_DAYS = int(os.getenv("SQUEEZE_LOOKBACK_DAYS", "5"))

SQUEEZE_MIN_PRICE = float(os.getenv("SQUEEZE_MIN_PRICE", "5"))
SQUEEZE_MIN_DAY_MOVE_PCT = float(os.getenv("SQUEEZE_MIN_DAY_MOVE_PCT", "8"))
SQUEEZE_MIN_INTRADAY_FROM_OPEN_PCT = float(
    os.getenv("SQUEEZE_MIN_INTRADAY_FROM_OPEN_PCT", "5")
)
SQUEEZE_MIN_RVOL_EQUITY = float(os.getenv("SQUEEZE_MIN_RVOL_EQUITY", "2"))
SQUEEZE_MIN_DOLLAR_VOL = float(os.getenv("SQUEEZE_MIN_DOLLAR_VOL", "5000000"))
SQUEEZE_MAX_FROM_HIGH_PCT = float(os.getenv("SQUEEZE_MAX_FROM_HIGH_PCT", "3"))

# ---------------- STATE ----------------

_alert_date: Optional[date] = None
_alerted_syms: set[str] = set()


def _reset_day() -> None:
    """Reset per-day de-duplication for alerts."""

    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    return sym in _alerted_syms


def _mark(sym: str) -> None:
    _alerted_syms.add(sym)


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _bar_date(bar: Any) -> Optional[date]:
    ts = getattr(bar, "timestamp", getattr(bar, "t", None))
    if ts is None:
        return None
    if ts > 1e12:
        ts /= 1000.0
    try:
        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        return dt_utc.astimezone(eastern).date()
    except Exception:
        return None


def _fetch_daily_bars(sym: str, days: int) -> List[Any]:
    """Fetch recent daily bars (ascending order)."""

    if not _client:
        return []
    try:
        end = date.today()
        start = end - timedelta(days=days + 3)
        bars = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start.isoformat(),
                to=end.isoformat(),
                limit=days + 5,
                sort="asc",
            )
        )
        return bars
    except Exception as exc:
        print(f"[squeeze] daily agg error for {sym}: {exc}")
        return []


def _compute_metrics(sym: str, trading_day: date) -> Optional[Dict[str, float]]:
    """Compute price/volume metrics required for squeeze detection."""

    daily = _fetch_daily_bars(sym, SQUEEZE_LOOKBACK_DAYS + 1)
    if len(daily) < 2:
        return None

    today_bar = daily[-1]
    if _bar_date(today_bar) != trading_day:
        return None

    prev_bar = daily[-2]
    last_price = _safe_float(getattr(today_bar, "close", getattr(today_bar, "c", None)))
    open_today = _safe_float(getattr(today_bar, "open", getattr(today_bar, "o", None)))
    day_high = _safe_float(getattr(today_bar, "high", getattr(today_bar, "h", None)))
    day_low = _safe_float(getattr(today_bar, "low", getattr(today_bar, "l", None)))
    vol_today = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))
    prev_close = _safe_float(getattr(prev_bar, "close", getattr(prev_bar, "c", None)))

    if None in (last_price, open_today, day_high, day_low, vol_today, prev_close):
        return None
    if any(val <= 0 for val in (last_price, open_today, prev_close, vol_today)):
        return None

    move_pct = (last_price - prev_close) / prev_close * 100.0
    from_open_pct = (last_price - open_today) / open_today * 100.0
    dollar_vol = last_price * vol_today

    hist = daily[:-1]
    recent_hist = hist[-SQUEEZE_LOOKBACK_DAYS :]
    if recent_hist:
        avg_vol = sum(
            float(getattr(bar, "volume", getattr(bar, "v", 0.0))) for bar in recent_hist
        ) / float(len(recent_hist))
    else:
        avg_vol = vol_today
    rvol = vol_today / avg_vol if avg_vol > 0 else 1.0

    high_close_distance_pct = ((day_high - last_price) / day_high * 100.0) if day_high else 100.0

    return {
        "last_price": last_price,
        "open_today": open_today,
        "day_high": day_high,
        "day_low": day_low,
        "vol_today": vol_today,
        "move_pct": move_pct,
        "from_open_pct": from_open_pct,
        "dollar_vol": dollar_vol,
        "rvol": rvol,
        "high_close_distance_pct": high_close_distance_pct,
    }


def _format_time() -> str:
    ts = now_est()
    if isinstance(ts, datetime):
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    return str(ts)


# ---------------- MAIN BOT ----------------

async def run_squeeze() -> None:
    """
    Scan liquid equities for short-squeeze style moves:
      • Big day move from prior close and from open
      • High RVOL and dollar volume
      • Closing near the high of day
    """

    _reset_day()
    start_ts = time.perf_counter()
    scanned = 0
    matches = 0
    alerts = 0
    reason_counts: Dict[str, int] = defaultdict(int)

    if not _client:
        print("[squeeze] missing data client; skipping run")
        record_bot_stats(BOT_NAME, scanned, matches, alerts, 0.0)
        return

    if not SQUEEZE_ALLOW_OUTSIDE_RTH and not in_rth_window_est():
        print("[squeeze] outside RTH; skipping")
        runtime = time.perf_counter() - start_ts
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
        return

    universe = resolve_universe_for_bot(
        bot_name=BOT_NAME,
        bot_env_var="SQUEEZE_TICKER_UNIVERSE",
        base_env_universe="TICKER_UNIVERSE",
        max_universe_env="SQUEEZE_MAX_UNIVERSE",
        default_max_universe=SQUEEZE_MAX_UNIVERSE,
        apply_dynamic_filters=True,
    )
    if not universe:
        print("[squeeze] empty universe; skipping")
        runtime = time.perf_counter() - start_ts
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
        return

    print(f"[squeeze] universe_size={len(universe)}")

    ts_now = now_est()
    trading_day = ts_now.date() if isinstance(ts_now, datetime) else date.today()
    min_dollar_vol = max(SQUEEZE_MIN_DOLLAR_VOL, float(os.getenv("TREND_RIDER_MIN_DOLLAR_VOL", "0")))

    for sym in universe:
        scanned += 1
        try:
            if is_etf_blacklisted(sym):
                debug_filter_reason(BOT_NAME, sym, "etf_blacklist")
                reason_counts["etf_blacklist"] += 1
                continue
            if _already_alerted(sym):
                debug_filter_reason(BOT_NAME, sym, "already_alerted")
                reason_counts["already_alerted"] += 1
                continue

            metrics = _compute_metrics(sym, trading_day)
            if not metrics:
                debug_filter_reason(BOT_NAME, sym, "no_data")
                reason_counts["no_data"] += 1
                continue

            price = metrics["last_price"]
            if price < SQUEEZE_MIN_PRICE:
                debug_filter_reason(BOT_NAME, sym, "price_below_min")
                reason_counts["price_below_min"] += 1
                continue

            dollar_vol = metrics["dollar_vol"]
            if dollar_vol < min_dollar_vol:
                debug_filter_reason(BOT_NAME, sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] += 1
                continue

            if metrics["vol_today"] < MIN_VOLUME_GLOBAL:
                debug_filter_reason(BOT_NAME, sym, "share_volume_too_low")
                reason_counts["share_volume_too_low"] += 1
                continue

            rvol = metrics["rvol"]
            if rvol < max(MIN_RVOL_GLOBAL, SQUEEZE_MIN_RVOL_EQUITY):
                debug_filter_reason(BOT_NAME, sym, "rvol_too_low")
                reason_counts["rvol_too_low"] += 1
                continue

            move_pct = metrics["move_pct"]
            if move_pct < SQUEEZE_MIN_DAY_MOVE_PCT:
                debug_filter_reason(BOT_NAME, sym, "day_move_too_small")
                reason_counts["day_move_too_small"] += 1
                continue

            from_open = metrics["from_open_pct"]
            if from_open < SQUEEZE_MIN_INTRADAY_FROM_OPEN_PCT:
                debug_filter_reason(BOT_NAME, sym, "intraday_move_too_small")
                reason_counts["intraday_move_too_small"] += 1
                continue

            if metrics["high_close_distance_pct"] > SQUEEZE_MAX_FROM_HIGH_PCT:
                debug_filter_reason(BOT_NAME, sym, "far_from_high_of_day")
                reason_counts["far_from_high_of_day"] += 1
                continue

            matches += 1
            body_lines = [
                f"SQUEEZE RADAR — {sym}",
                f"• Last: ${price:.2f} (+{move_pct:.1f}% vs close, +{from_open:.1f}% from open)",
                f"• Volume: {int(metrics['vol_today']):,} ({rvol:.1f}× avg) — Dollar Vol: ${dollar_vol:,.0f}",
                f"• Near HOD: {metrics['high_close_distance_pct']:.1f}% off high",
                "• Context: Strong up move with heavy volume; potential squeeze continuation.",
                f"• Chart: {chart_link(sym)}",
            ]
            send_alert(BOT_NAME, sym, price, rvol, extra="\n".join(body_lines))
            alerts += 1
            _mark(sym)
        except Exception as exc:
            debug_filter_reason(BOT_NAME, sym, "error")
            reason_counts["error"] += 1
            print(f"[squeeze] ERROR for {sym}: {exc}")
            continue

    runtime = time.perf_counter() - start_ts
    try:
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
    except Exception as exc:
        print(f"[squeeze] record_bot_stats error: {exc}")

    if matches == 0:
        print(f"[squeeze] No alerts. Filter breakdown: {dict(reason_counts)}")
    print(
        f"[squeeze] done scanned={scanned} matches={matches} alerts={alerts} runtime={runtime:.2f}s"
    )