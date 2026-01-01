# bots/dark_pool_radar.py — OPTIMIZED / MORE ALERTS / POLYGON-SAFE

import os
import time
from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Tuple

import pytz

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover - fallback for environments without massive
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    chart_link,
    format_est_timestamp,
    in_rth_window_est,
    is_etf_blacklisted,
    now_est_dt,
    resolve_universe_for_bot,
    send_alert_text,
)
from bots.status_report import record_bot_stats, record_error

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

# Exchanges considered "dark" / ATS-like
DARK_EXCHANGES = {8, 9, 80, 81, 82, 84, 87, 88, 201, 202}

# Window + thresholds (new names with backward-compatible fallbacks)
DARK_POOL_LOOKBACK_MINUTES = int(os.getenv("DARK_POOL_LOOKBACK_MINUTES", os.getenv("DARK_LOOKBACK_MIN", "30")))
DARK_POOL_MIN_NOTIONAL = float(os.getenv("DARK_POOL_MIN_NOTIONAL", os.getenv("DARK_MIN_TOTAL_NOTIONAL", "1000000")))
DARK_POOL_MIN_LARGEST_PRINT = float(os.getenv("DARK_POOL_MIN_LARGEST_PRINT", os.getenv("DARK_MIN_SINGLE_NOTIONAL", "500000")))
DARK_POOL_MIN_PRINTS = int(os.getenv("DARK_POOL_MIN_PRINTS", os.getenv("DARK_MIN_PRINT_COUNT", "1")))
DARK_POOL_MIN_DOLLAR_VOL = float(os.getenv("DARK_POOL_MIN_DOLLAR_VOL", os.getenv("DARK_MIN_DOLLAR_VOL", "10000000")))
DARK_POOL_MIN_RVOL = max(MIN_RVOL_GLOBAL, float(os.getenv("DARK_POOL_MIN_RVOL", os.getenv("DARK_MIN_RVOL", "1.0"))))
DARK_POOL_ALLOW_OUTSIDE_RTH = os.getenv("DARK_POOL_ALLOW_OUTSIDE_RTH", "false").lower() == "true"

# ------------------- STATE -------------------

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


def _mark(sym: str) -> None:
    _reset_if_new_day()
    _alerted.add(sym)


def _safe(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _get_trade_ts(trade: Any) -> Optional[datetime]:
    ts_fields = ["sip_timestamp", "trf_timestamp", "participant_timestamp", "timestamp"]
    for field in ts_fields:
        raw = _safe(trade, field, None)
        if raw:
            try:
                # Polygon returns ns; convert to seconds
                if raw > 1e12:
                    raw = raw / 1_000_000_000
                return datetime.fromtimestamp(raw, tz=eastern)
            except Exception:
                continue
    return None


def _dark_pool_window(now_et: datetime) -> Tuple[datetime, datetime]:
    start = now_et - timedelta(minutes=DARK_POOL_LOOKBACK_MINUTES)
    today_open = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if start < today_open:
        start = today_open
    return start, now_et


def _resolve_universe() -> List[str]:
    return resolve_universe_for_bot(
        bot_name="dark_pool_radar",
        bot_env_var="DARK_POOL_TICKER_UNIVERSE",
        max_universe_env="DARK_POOL_MAX_UNIVERSE",
        default_max_universe=None,
        apply_dynamic_filters=True,
    )


def _format_context(move_pct: float, dist_from_low: Optional[float], vwap_rel: Optional[float]) -> str:
    if dist_from_low is not None and move_pct <= -3 and (dist_from_low <= 1.5 or (vwap_rel is not None and vwap_rel < -0.2)):
        return "Heavy selloff with blocks near lows below VWAP (capitulation-style)."
    if vwap_rel is not None and vwap_rel > 0.2:
        return "Blocks printing while price holds above VWAP (potential accumulation)."
    if dist_from_low is not None and dist_from_low < 3:
        return "Prints clustering near session lows; watch for stabilizing bids."
    return "Mid-range dark pool activity; monitor follow-through."


# ------------------- MAIN BOT -------------------


async def run_dark_pool_radar() -> None:
    BOT_NAME = "dark_pool_radar"
    start_dt = now_est_dt()
    scanned = 0
    matches = 0
    alerts = 0

    if not POLYGON_KEY or not _client:
        record_error(BOT_NAME, Exception("Missing POLYGON_KEY or client"))
        record_bot_stats(BOT_NAME, scanned=0, matched=0, alerts=0, started_at=start_dt, finished_at=now_est_dt())
        return

    if not DARK_POOL_ALLOW_OUTSIDE_RTH and not in_rth_window_est():
        record_bot_stats(BOT_NAME, scanned=0, matched=0, alerts=0, started_at=start_dt, finished_at=now_est_dt())
        return

    universe = _resolve_universe()
    now_et = now_est_dt()
    start_window, end_window = _dark_pool_window(now_et)
    today = now_et.date()
    today_iso = today.isoformat()

    for sym in universe:
        scanned += 1
        if is_etf_blacklisted(sym) or _already(sym):
            continue

        try:
            # Daily bars to derive last, prev close, volume, high/low, vwap
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=60)).isoformat(),
                    to=today_iso,
                    limit=60,
                )
            )
        except Exception as exc:
            record_error(BOT_NAME, Exception(f"daily fetch failed for {sym}: {exc}"))
            continue

        if len(days) < 5:
            continue

        try:
            today_bar = days[-1]
            prev_bar = days[-2]
            last_price = float(getattr(today_bar, "close", getattr(today_bar, "c", 0.0)))
            prev_close = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0.0)))
            day_vol = float(getattr(today_bar, "volume", getattr(today_bar, "v", 0.0)))
            day_high = float(getattr(today_bar, "high", getattr(today_bar, "h", 0.0)) or 0.0)
            day_low = float(getattr(today_bar, "low", getattr(today_bar, "l", 0.0)) or 0.0)
            day_open = float(getattr(today_bar, "open", getattr(today_bar, "o", 0.0)) or 0.0)
            day_vwap = float(getattr(today_bar, "vw", 0.0) or 0.0)
        except Exception:
            continue

        if last_price <= 0 or day_vol <= 0:
            continue

        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < DARK_POOL_MIN_DOLLAR_VOL:
            continue

        vols = [float(getattr(d, "volume", getattr(d, "v", 0.0))) for d in days[-21:-1]]
        avg_vol = sum(vols) / max(len(vols), 1)
        if avg_vol <= 0:
            continue
        rvol = day_vol / avg_vol
        if rvol < DARK_POOL_MIN_RVOL:
            continue

        move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0

        # Dark pool prints for today only within the lookback window
        total_notional = 0.0
        largest_notional = 0.0
        largest_price = 0.0
        trade_count = 0

        try:
            trades = _client.list_trades(
                ticker=sym,
                timestamp_gte=int(start_window.timestamp() * 1_000_000_000),
                timestamp_lte=int(end_window.timestamp() * 1_000_000_000),
                limit=5000,
            )
        except Exception as exc:
            record_error(BOT_NAME, Exception(f"trades fetch failed for {sym}: {exc}"))
            continue

        for trade in trades:
            ex = _safe(trade, "exchange", None)
            if ex not in DARK_EXCHANGES:
                continue

            ts = _get_trade_ts(trade)
            if not ts or ts.date() != today:
                continue
            if ts < start_window or ts > end_window:
                continue

            price = float(_safe(trade, "price", 0.0) or 0.0)
            size = float(_safe(trade, "size", 0.0) or 0.0)
            if price <= 0 or size <= 0:
                continue

            notional = price * size
            total_notional += notional
            trade_count += 1
            if notional > largest_notional:
                largest_notional = notional
                largest_price = price

        if trade_count < DARK_POOL_MIN_PRINTS:
            continue
        if total_notional < DARK_POOL_MIN_NOTIONAL and largest_notional < DARK_POOL_MIN_LARGEST_PRINT:
            continue

        # At this point, sym is interesting
        matches += 1
        _mark(sym)

        dist_from_low = None
        if day_low > 0:
            dist_from_low = max((last_price - day_low) / day_low * 100.0, 0.0)
        vwap_rel = None
        if day_vwap > 0:
            vwap_rel = (last_price - day_vwap) / day_vwap * 100.0
        context = _format_context(move_pct, dist_from_low, vwap_rel)
        dp_share_pct = None
        if dollar_vol > 0:
            dp_share_pct = max(min(total_notional / dollar_vol * 100.0, 300.0), 0.0)

        header = [
            f"🕳️ DARK POOL RADAR — {sym}",
            format_est_timestamp(now_et),
            f"💰 Underlying: ${last_price:.2f} · Day Move: {move_pct:+.1f}% · RVOL: {rvol:.1f}x",
            "────────────",
            f"🧊 Window: last {DARK_POOL_LOOKBACK_MINUTES} min (today only)",
            f"📦 Prints: {trade_count:,}",
            f"💵 Dark Pool Notional (window): ≈ ${total_notional:,.0f}",
            f"🐋 Largest Print: ≈ ${largest_notional:,.0f} @ ${largest_price:.2f}" if largest_notional > 0 else None,
        ]
        if dp_share_pct is not None:
            header.append(f"📊 Dark Pool vs Full-Day Volume: {dp_share_pct:.1f}% of today’s $ volume")
        header.extend(
            [
                f"🔍 Context: {context}",
                f"🔗 Chart: {chart_link(sym)}",
            ]
        )

        alert_text = "\n".join([line for line in header if line])
        send_alert_text(alert_text)
        alerts += 1

    finished_dt = now_est_dt()
    runtime = max((finished_dt - start_dt).total_seconds(), 0.0)
    record_bot_stats(
        BOT_NAME,
        scanned=scanned,
        matched=matches,
        alerts=alerts,
        started_at=start_dt,
        finished_at=finished_dt,
    )
