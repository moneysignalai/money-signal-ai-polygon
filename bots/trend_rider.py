"""Trend Rider bot

Scans for equities already in strong uptrends that are breaking out above
recent ranges with supportive volume. RTH-gated by default and uses the shared
top-volume universe resolver so scan counts stay consistent across bots.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from statistics import mean
from typing import List, Tuple

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover - local fallback
    from polygon import RESTClient

from bots.shared import (
    DEBUG_FLOW_REASONS,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    POLYGON_KEY,
    chart_link,
    debug_filter_reason,
    in_rth_window_est,
    now_est_dt,
    resolve_universe_for_bot,
    send_alert_text,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "trend_rider"

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

_allow_outside_rth = os.getenv("TREND_RIDER_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
_min_price = float(os.getenv("TREND_RIDER_MIN_PRICE", "5"))
_min_dollar_vol = float(os.getenv("TREND_RIDER_MIN_DOLLAR_VOL", "150000"))
_min_rvol = float(os.getenv("TREND_RIDER_MIN_RVOL", "0.9"))
_trend_days = int(os.getenv("TREND_RIDER_TREND_DAYS", "10"))
_min_breakout_pct = float(os.getenv("TREND_RIDER_MIN_BREAKOUT_PCT", "1.0"))
_min_move_pct = float(os.getenv("TREND_RIDER_MIN_MOVE_PCT", "3.0"))
_max_universe = int(os.getenv("TREND_RIDER_MAX_UNIVERSE", os.getenv("DYNAMIC_MAX_TICKERS", "2000")))
_breakout_lookback = int(os.getenv("TREND_RIDER_BREAKOUT_LOOKBACK", "20"))
_lookback_days = int(os.getenv("TREND_RIDER_LOOKBACK_DAYS", "70"))


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
        print(f"[trend_rider] daily agg error for {sym}: {exc}")
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


async def run_trend_rider() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not _allow_outside_rth and not in_rth_window_est():
            print("[trend_rider] outside RTH; skipping")
            return record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)

        universe = resolve_universe_for_bot(
            bot_name=BOT_NAME,
            max_universe_env="TREND_RIDER_MAX_UNIVERSE",
            default_max_universe=_max_universe,
        )
        print(f"[trend_rider] universe_size={len(universe)}")
        if not universe:
            record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
            return

        for sym in universe:
            scanned += 1
            try:
                daily = _fetch_daily(sym, max(_lookback_days, 60))
            except Exception as exc:
                print(f"[trend_rider] data error for {sym}: {exc}")
                record_error(BOT_NAME, exc)
                continue

            if len(daily) < 50:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "insufficient_history")
                reason_counts["insufficient_history"] = reason_counts.get(
                    "insufficient_history", 0
                ) + 1
                continue

            opens: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            closes: list[float] = []
            volumes: list[float] = []
            for bar in daily:
                o, h, l, c, v = _extract_ohlcv(bar)
                if c > 0 and v > 0:
                    opens.append(o)
                    closes.append(c)
                    lows.append(l)
                    highs.append(h)
                    volumes.append(v)

            if len(closes) < 50:
                continue

            close_today = closes[-1]
            open_today = opens[-1] if opens else 0.0
            high_today = highs[-1] if highs else 0.0
            low_today = lows[-1] if lows else 0.0
            prev_low = lows[-2] if len(lows) >= 2 else 0.0
            volume_today = volumes[-1]
            ma20 = _moving_average(closes, 20)
            ma50 = _moving_average(closes, 50)
            ma200 = _moving_average(closes, 200) if len(closes) >= 200 else 0.0
            prev_close = closes[-2] if len(closes) >= 2 else 0.0

            avg_vol = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)
            rvol = volume_today / avg_vol if avg_vol > 0 else 0.0
            dollar_vol = close_today * volume_today
            day_change_pct = (
                (close_today - prev_close) / prev_close * 100 if prev_close > 0 else 0.0
            )

            lookback_window = max(_breakout_lookback, _trend_days)
            lookback_highs = highs[:-1][-lookback_window:] if len(highs) > 1 else []
            recent_high = max(lookback_highs) if lookback_highs else 0.0

            if close_today < _min_price:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "price_too_low")
                reason_counts["price_too_low"] = reason_counts.get("price_too_low", 0) + 1
                continue

            if dollar_vol < max(_min_dollar_vol, MIN_VOLUME_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] = reason_counts.get(
                    "dollar_vol_too_low", 0
                ) + 1
                continue

            if rvol < max(_min_rvol, MIN_RVOL_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "rvol_too_low")
                reason_counts["rvol_too_low"] = reason_counts.get("rvol_too_low", 0) + 1
                continue

            if ma200 == 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "insufficient_ma200")
                reason_counts["insufficient_ma200"] = reason_counts.get(
                    "insufficient_ma200", 0
                ) + 1
                continue

            in_trend = close_today >= ma50 > 0 and close_today >= ma200 > 0 and ma20 > ma50
            if not in_trend:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "not_in_uptrend")
                reason_counts["not_in_uptrend"] = reason_counts.get("not_in_uptrend", 0) + 1
                continue

            breakout_level = recent_high * (1 + _min_breakout_pct / 100)
            if recent_high <= 0 or close_today < breakout_level:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "breakout_not_confirmed")
                reason_counts["breakout_not_confirmed"] = reason_counts.get(
                    "breakout_not_confirmed", 0
                ) + 1
                continue

            if day_change_pct < _min_move_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "move_too_small")
                reason_counts["move_too_small"] = reason_counts.get("move_too_small", 0) + 1
                continue

            matches += 1
            breakout_diff_pct = (
                ((close_today - recent_high) / recent_high * 100) if recent_high > 0 else 0.0
            )
            direction_label = "UP" if day_change_pct >= 0 else "DOWN"

            ma50_status = "above 50SMA" if close_today >= ma50 > 0 else "below 50SMA"
            ma200_status = "above 200SMA" if close_today >= ma200 > 0 else "below 200SMA"
            trend_read = "Mixed structure; monitor breakout follow-through."
            if close_today >= ma50 and (ma200 == 0 or close_today >= ma200):
                if breakout_diff_pct > 0:
                    trend_read = "Strong trend, stacked MAs, fresh breakout."
                else:
                    trend_read = "Uptrend continuation with MA support."
            elif close_today >= ma50 and ma200 > 0 and close_today < ma200:
                trend_read = "Recovery attempt in longer-term downtrend."

            rvol_text = f"{rvol:.1f}×" if rvol > 0 else "N/A"
            dollar_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"

            ts_dt = now_est_dt()
            ts_display = ts_dt.strftime("%I:%M %p EST · %m-%d-%Y").lstrip("0")
            vwap_today = float(
                getattr(daily[-1], "vwap", getattr(daily[-1], "vw", 0.0)) or 0.0
            )
            vwap_relation = "ABOVE" if vwap_today and close_today >= vwap_today else "BELOW"
            volume_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"

            header_lines = [
                f"🚀 TREND RIDER — {sym}",
                f"🕒 {ts_display}",
                "",
                "💰 Price + Move",
                f"• Last: ${close_today:,.2f} ({day_change_pct:+.1f}% {direction_label})",
                (
                    f"• O ${open_today:,.2f} · H ${high_today:,.2f} · "
                    f"L ${low_today:,.2f} · C ${close_today:,.2f}"
                ),
                f"• RVOL: {rvol_text} · Dollar Vol: {volume_text}",
                "",
                "📈 Trend Structure",
                f"• Above 50-day MA: {'YES' if close_today >= ma50 > 0 else 'NO'}",
                f"• Above 200-day MA: {'YES' if close_today >= ma200 > 0 else 'NO'}",
                f"• Breakout vs {_breakout_lookback}-day high: ${recent_high:,.2f}",
                f"• Intraday vs VWAP: {vwap_relation if vwap_today else 'N/A'}",
                "",
                "🧠 Read",
                trend_read,
                "",
                "🔗 Chart",
                chart_link(sym, timeframe="D"),
            ]

            alert_text = "\n".join(header_lines)
            try:
                send_alert_text(alert_text)
                alerts += 1
            except Exception as exc:
                print(f"[trend_rider] alert error for {sym}: {exc}")
                record_error(BOT_NAME, exc)

        if matches == 0 and DEBUG_FLOW_REASONS:
            print(f"[trend_rider] No alerts. Filter breakdown: {reason_counts}")
    except Exception as exc:
        print(f"[trend_rider] error: {exc}")
        record_error(BOT_NAME, exc)
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)


async def run_bot() -> None:  # legacy alias
    await run_trend_rider()

