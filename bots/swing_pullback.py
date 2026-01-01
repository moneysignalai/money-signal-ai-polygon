"""Swing Pullback bot

Identifies up-trending equities pulling back toward moving averages with
healthy liquidity. The bot is RTH-gated by default and reports clear reasons
when symbols are filtered out while always recording stats for heartbeat.
"""

import os
import time
from datetime import date, timedelta
from statistics import mean
from typing import List, Tuple

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover
    from polygon import RESTClient

from bots.shared import (
    DEBUG_FLOW_REASONS,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    POLYGON_KEY,
    chart_link,
    debug_filter_reason,
    in_rth_window_est,
    resolve_universe_for_bot,
    send_alert,
)
from bots.status_report import record_bot_stats

BOT_NAME = "swing_pullback"

_allow_outside_rth = os.getenv("SWING_PULLBACK_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
_min_price = float(os.getenv("SWING_MIN_PRICE", "5"))
_min_trend_days = int(os.getenv("SWING_MIN_TREND_DAYS", "10"))
_min_pullback_pct = float(os.getenv("SWING_MIN_PULLBACK_PCT", "3"))
_max_pullback_pct = float(os.getenv("SWING_MAX_PULLBACK_PCT", "10"))
_min_rvol = float(os.getenv("SWING_MIN_RVOL", "1.0"))
_min_dollar_vol = float(os.getenv("SWING_MIN_DOLLAR_VOL", os.getenv("TREND_RIDER_MIN_DOLLAR_VOL", "150000")))
_max_universe = int(os.getenv("SWING_PULLBACK_MAX_UNIVERSE", os.getenv("DYNAMIC_MAX_TICKERS", "2000")))
_lookback_days = int(os.getenv("SWING_LOOKBACK_DAYS", "60"))

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None


def _fetch_daily(sym: str, days: int) -> List:
    if not _client:
        return []
    start = (date.today() - timedelta(days=days + 5)).isoformat()
    end = date.today().isoformat()
    try:
        return list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=days + 10,
                sort="asc",
            )
        )
    except Exception as exc:
        print(f"[swing_pullback] daily agg error for {sym}: {exc}")
        return []


def _extract_ohlcv(bar: any) -> Tuple[float, float, float, float, float]:
    open_ = float(getattr(bar, "open", getattr(bar, "o", 0.0)) or 0.0)
    high = float(getattr(bar, "high", getattr(bar, "h", 0.0)) or 0.0)
    low = float(getattr(bar, "low", getattr(bar, "l", 0.0)) or 0.0)
    close = float(getattr(bar, "close", getattr(bar, "c", 0.0)) or 0.0)
    volume = float(getattr(bar, "volume", getattr(bar, "v", 0.0)) or 0.0)
    return open_, high, low, close, volume


def _moving_average(values: List[float], window: int) -> float:
    recent = values[-window:]
    if not recent:
        return 0.0
    return sum(recent) / len(recent)


def _swing_pullback_metrics(daily: List) -> Tuple[float, float, float, float, float, float]:
    today_bar = daily[-1]
    closes = [_extract_ohlcv(b)[3] for b in daily if _extract_ohlcv(b)[3] > 0]
    volumes = [_extract_ohlcv(b)[4] for b in daily if _extract_ohlcv(b)[4] > 0]

    if len(closes) < max(50, _min_trend_days + 5):
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    close_today = closes[-1]
    volume_today = volumes[-1]
    ma20 = _moving_average(closes, 20)
    ma50 = _moving_average(closes, 50)
    swing_high = max(closes[-_min_trend_days:]) if len(closes) >= _min_trend_days else max(closes)
    pullback_pct = (swing_high - close_today) / swing_high * 100 if swing_high > 0 else 0.0

    avg_vol = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)
    rvol = volume_today / avg_vol if avg_vol > 0 else 0.0
    dollar_vol = close_today * volume_today
    return close_today, dollar_vol, rvol, pullback_pct, ma20, ma50


async def run_swing_pullback() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not _allow_outside_rth and not in_rth_window_est():
            print("[swing_pullback] outside RTH; skipping")
            return

        universe = resolve_universe_for_bot(
            bot_name="swing_pullback",
            max_universe_env="SWING_PULLBACK_MAX_UNIVERSE",
            default_max_universe=_max_universe,
        )
        print(f"[swing_pullback] universe_size={len(universe)}")
        if not universe:
            return

        for sym in universe:
            scanned += 1
            try:
                daily = _fetch_daily(sym, max(_lookback_days, 70))
            except Exception as exc:
                print(f"[swing_pullback] data error for {sym}: {exc}")
                continue

            if len(daily) < 50:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "insufficient_history")
                reason_counts["insufficient_history"] = reason_counts.get(
                    "insufficient_history", 0
                ) + 1
                continue

            close_today, dollar_vol, rvol, pullback_pct, ma20, ma50 = _swing_pullback_metrics(daily)
            if close_today <= 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "bad_prices")
                reason_counts["bad_prices"] = reason_counts.get("bad_prices", 0) + 1
                continue

            if close_today < _min_price:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "price_too_low")
                reason_counts["price_too_low"] = reason_counts.get("price_too_low", 0) + 1
                continue

            if dollar_vol < max(_min_dollar_vol, MIN_VOLUME_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] = reason_counts.get(
                    "dollar_vol_too_low", 0
                ) + 1
                continue

            if rvol < max(_min_rvol, MIN_RVOL_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "rvol_too_low")
                reason_counts["rvol_too_low"] = reason_counts.get("rvol_too_low", 0) + 1
                continue

            in_uptrend = close_today > ma20 > ma50
            if not in_uptrend:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "not_in_uptrend")
                reason_counts["not_in_uptrend"] = reason_counts.get("not_in_uptrend", 0) + 1
                continue

            if pullback_pct < _min_pullback_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "pullback_too_shallow")
                reason_counts["pullback_too_shallow"] = reason_counts.get(
                    "pullback_too_shallow", 0
                ) + 1
                continue

            if pullback_pct > _max_pullback_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("swing_pullback", sym, "pullback_too_deep")
                reason_counts["pullback_too_deep"] = reason_counts.get("pullback_too_deep", 0) + 1
                continue

            matches += 1
            text = (
                f"SWING PULLBACK — {sym}\n"
                f"• Price: ${close_today:.2f} (pullback {pullback_pct:.1f}% from swing high)\n"
                f"• MAs: MA20 {ma20:.2f} | MA50 {ma50:.2f}\n"
                f"• Volume: {rvol:.1f}× avg — Dollar Vol ${dollar_vol:,.0f}\n"
                f"• Link: {chart_link(sym, timeframe='D')}"
            )
            try:
                send_alert(text)
                alerts += 1
            except Exception as exc:
                print(f"[swing_pullback] alert error for {sym}: {exc}")

        if matches == 0 and DEBUG_FLOW_REASONS:
            print(f"[swing_pullback] No alerts. Filter breakdown: {reason_counts}")
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
