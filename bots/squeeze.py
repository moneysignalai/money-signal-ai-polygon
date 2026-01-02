"""
Squeeze bot
-----------

Volatility compression → breakout detector. Without any short-interest feed,
this bot looks for coiled ranges (narrow Bollinger widths, tight intraday
ranges, modest RVOL) that start to resolve with expanding volume. Alerts are
purely presentation upgrades; core selection remains env-driven.
"""

from collections import defaultdict
import os
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    format_est_timestamp,
    in_rth_window_est,
    is_etf_blacklisted,
    now_est_dt,
    resolve_universe_for_bot,
    send_alert_text,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

BOT_NAME = "squeeze"
STRATEGY_TAG = "SQUEEZE_BREAK"

SQUEEZE_MAX_UNIVERSE = int(os.getenv("SQUEEZE_MAX_UNIVERSE", "2000"))
SQUEEZE_ALLOW_OUTSIDE_RTH = (
    os.getenv("SQUEEZE_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
)
SQUEEZE_LOOKBACK_DAYS = int(os.getenv("SQUEEZE_LOOKBACK_DAYS", "5"))
SQUEEZE_COMPRESSION_LOOKBACK = int(os.getenv("SQUEEZE_COMPRESSION_LOOKBACK", "5"))
SQUEEZE_BB_PERIOD = int(os.getenv("SQUEEZE_BB_PERIOD", "20"))
SQUEEZE_BB_STD = float(os.getenv("SQUEEZE_BB_STD", "2"))
SQUEEZE_MAX_INTRADAY_RANGE_PCT = float(os.getenv("SQUEEZE_MAX_INTRADAY_RANGE_PCT", "3"))
SQUEEZE_BREAK_MIN_RVOL = float(os.getenv("SQUEEZE_BREAK_MIN_RVOL", "1.3"))

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

    min_days = max(SQUEEZE_BB_PERIOD + SQUEEZE_COMPRESSION_LOOKBACK + 2, SQUEEZE_LOOKBACK_DAYS + 2)
    daily = _fetch_daily_bars(sym, min_days)
    if len(daily) < SQUEEZE_BB_PERIOD + 1:
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

    closes = [
        _safe_float(getattr(bar, "close", getattr(bar, "c", None)))
        for bar in daily
    ]
    volumes = [
        _safe_float(getattr(bar, "volume", getattr(bar, "v", None)))
        for bar in daily
    ]
    closes = [c for c in closes if c is not None]
    volumes = [v for v in volumes if v is not None]
    if len(closes) < SQUEEZE_BB_PERIOD:
        return None

    # Bollinger Bands on the latest BB period (including today)
    window_closes = closes[-SQUEEZE_BB_PERIOD :]
    mid = statistics.fmean(window_closes)
    std = statistics.pstdev(window_closes)
    upper = mid + SQUEEZE_BB_STD * std
    lower = mid - SQUEEZE_BB_STD * std
    width_pct = (upper - lower) / mid * 100.0 if mid else None

    # Compression vs recent widths
    recent_widths: List[float] = []
    for idx in range(len(closes) - SQUEEZE_BB_PERIOD - 1, len(closes) - SQUEEZE_BB_PERIOD - 1 - SQUEEZE_COMPRESSION_LOOKBACK, -1):
        if idx < 0:
            break
        slice_closes = closes[idx : idx + SQUEEZE_BB_PERIOD]
        if len(slice_closes) < SQUEEZE_BB_PERIOD:
            continue
        mid_i = statistics.fmean(slice_closes)
        std_i = statistics.pstdev(slice_closes)
        if mid_i > 0:
            width_i = (mid_i + SQUEEZE_BB_STD * std_i - (mid_i - SQUEEZE_BB_STD * std_i)) / mid_i * 100.0
            recent_widths.append(width_i)

    avg_recent_width = statistics.fmean(recent_widths) if recent_widths else None
    compression_ok = False
    if width_pct is not None and avg_recent_width:
        compression_ok = width_pct <= avg_recent_width * 0.85

    intraday_range_pct = (
        (day_high - day_low) / last_price * 100.0 if last_price and day_high and day_low else None
    )
    if intraday_range_pct is not None and intraday_range_pct > SQUEEZE_MAX_INTRADAY_RANGE_PCT:
        compression_ok = False

    avg_recent_vol = None
    if len(volumes) >= 2:
        recent_vols = volumes[-(SQUEEZE_LOOKBACK_DAYS + 1) : -1]
        if recent_vols:
            avg_recent_vol = statistics.fmean(recent_vols)
    rvol = vol_today / avg_recent_vol if avg_recent_vol and avg_recent_vol > 0 else 1.0

    breakout_dir: Optional[str] = None
    if width_pct is not None and upper and lower:
        if last_price >= upper:
            breakout_dir = "UP"
        elif last_price <= lower:
            breakout_dir = "DOWN"

    recent_high = max(closes[-SQUEEZE_LOOKBACK_DAYS:], default=last_price)

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
        "width_pct": width_pct,
        "avg_recent_width": avg_recent_width,
        "compression_ok": compression_ok,
        "intraday_range_pct": intraday_range_pct,
        "upper_band": upper,
        "lower_band": lower,
        "breakout_dir": breakout_dir,
        "recent_high": recent_high,
    }


def _format_time(ts: Optional[datetime] = None) -> str:
    return format_est_timestamp(ts)


def _format_alert(sym: str, metrics: Dict[str, Any]) -> str:
    ts = _format_time(now_est_dt())
    last = metrics.get("last_price")
    move_pct = metrics.get("move_pct")
    open_today = metrics.get("open_today")
    day_high = metrics.get("day_high")
    day_low = metrics.get("day_low")
    vol_today = metrics.get("vol_today")
    rvol = metrics.get("rvol")
    dollar_vol = metrics.get("dollar_vol")
    width_pct = metrics.get("width_pct")
    avg_recent_width = metrics.get("avg_recent_width")
    breakout_dir = metrics.get("breakout_dir")
    upper_band = metrics.get("upper_band")
    lower_band = metrics.get("lower_band")
    intraday_range_pct = metrics.get("intraday_range_pct")
    recent_high = metrics.get("recent_high")

    dir_label = "UPSIDE" if breakout_dir == "UP" else "DOWNSIDE"
    band_line = (
        f"• Break direction: {dir_label} (close {'above' if breakout_dir == 'UP' else 'below'} band)"
        if breakout_dir
        else "• Break direction: n/a"
    )
    compression_line = (
        f"• Bollinger Band Width: {width_pct:.2f}% (recent avg {avg_recent_width:.2f}% )"
        if width_pct is not None and avg_recent_width is not None
        else "• Bollinger Band Width: n/a"
    )
    range_line = (
        f"• Daily range compression flagged over {SQUEEZE_COMPRESSION_LOOKBACK} sessions"
    )
    intraday_line = (
        f"• Intraday range: {intraday_range_pct:.2f}% of price"
        if intraday_range_pct is not None
        else "• Intraday range: n/a"
    )
    vwap_line = "• Above VWAP: n/a"

    return "\n".join(
        [
            f"🔥 SQUEEZE BREAKOUT — {sym}",
            f"🕒 {ts}",
            "",
            "💰 Price Snapshot",
            f"• Last: ${last:.2f} ({move_pct:+.1f}% today)" if last is not None and move_pct is not None else "• Last: n/a",
            (
                f"• O ${open_today:.2f} · H ${day_high:.2f} · L ${day_low:.2f} · C ${last:.2f}"
                if None not in (open_today, day_high, day_low, last)
                else "• O/H/L/C: n/a"
            ),
            (
                f"• RVOL: {rvol:.1f}× · Volume: {int(vol_today):,} · Dollar Vol: ${dollar_vol:,.0f}"
                if None not in (rvol, vol_today, dollar_vol)
                else "• RVOL/Volume: n/a"
            ),
            "",
            "📉 Compression Phase",
            compression_line,
            range_line,
            intraday_line,
            "",
            "📈 Breakout Context",
            band_line,
            vwap_line,
            f"• Recent swing high: ${recent_high:.2f}" if recent_high else "• Recent swing high: n/a",
            "",
            "🧠 Read",
            "Volatility squeeze resolving with volume starting to expand — classic pre-breakout to breakout transition.",
            "",
            "🔗 Chart",
            chart_link(sym),
        ]
    )


# ---------------- MAIN BOT ----------------

async def run_squeeze() -> None:
    """
    Scan liquid equities for volatility compression resolving into a breakout:
      • Narrow Bollinger widths vs recent history
      • Tight intraday range and healthy (but not explosive) RVOL
      • Break of upper/lower band to signal expansion
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

    ts_now = now_est_dt()
    trading_day = ts_now.date() if isinstance(ts_now, datetime) else date.today()
    min_dollar_vol = max(SQUEEZE_MIN_DOLLAR_VOL, float(os.getenv("TREND_RIDER_MIN_DOLLAR_VOL", "0")))
    rvol_floor = max(MIN_RVOL_GLOBAL, SQUEEZE_BREAK_MIN_RVOL)

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
            vol_today = metrics["vol_today"]
            if dollar_vol < min_dollar_vol:
                debug_filter_reason(BOT_NAME, sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] += 1
                continue
            if vol_today < MIN_VOLUME_GLOBAL:
                debug_filter_reason(BOT_NAME, sym, "share_volume_too_low")
                reason_counts["share_volume_too_low"] += 1
                continue

            rvol = metrics["rvol"]
            if rvol < rvol_floor:
                debug_filter_reason(BOT_NAME, sym, "rvol_too_low")
                reason_counts["rvol_too_low"] += 1
                continue

            if not metrics.get("compression_ok"):
                debug_filter_reason(BOT_NAME, sym, "no_compression")
                reason_counts["no_compression"] += 1
                continue

            intraday_range = metrics.get("intraday_range_pct")
            if intraday_range is None:
                debug_filter_reason(BOT_NAME, sym, "no_intraday_range")
                reason_counts["no_intraday_range"] += 1
                continue

            breakout_dir = metrics.get("breakout_dir")
            if breakout_dir is None:
                debug_filter_reason(BOT_NAME, sym, "no_breakout")
                reason_counts["no_breakout"] += 1
                continue

            matches += 1
            send_alert_text(_format_alert(sym, metrics))
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