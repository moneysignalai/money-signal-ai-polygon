"""Panic Flush bot: flags capitulation-style selloffs with heavy volume.

Criteria (price/volume only):
- Large down day vs prior close (threshold set by env)
- High RVOL and dollar volume
- Trading near the low of day and below VWAP

Universe: top-volume equities (Polygon/Massive) with fallback to TICKER_UNIVERSE.
Runs during RTH unless PANIC_FLUSH_ALLOW_OUTSIDE_RTH is true.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import mean
from typing import List, Tuple

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover - fallback for local dev
    from polygon import RESTClient

from bots.shared import (
    DEBUG_FLOW_REASONS,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    POLYGON_KEY,
    chart_link,
    debug_filter_reason,
    format_est_timestamp,
    in_rth_window_est,
    resolve_universe_for_bot,
    send_alert_text,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "panic_flush"

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
_allow_outside_rth = os.getenv("PANIC_FLUSH_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
_min_price = float(os.getenv("PANIC_FLUSH_MIN_PRICE", "3.0"))
_min_dollar_vol = float(os.getenv("PANIC_FLUSH_MIN_DOLLAR_VOL", "150000"))
_min_rvol = float(os.getenv("PANIC_FLUSH_MIN_RVOL", "1.1"))
_max_from_low_pct = float(os.getenv("PANIC_FLUSH_MAX_FROM_LOW_PCT", "3.0"))
_avg_vol_lookback = int(os.getenv("PANIC_FLUSH_LOOKBACK_DAYS", os.getenv("DYNAMIC_MAX_LOOKBACK_DAYS", "5")))

_drop_env = os.getenv("PANIC_FLUSH_MIN_DAY_DROP_PCT")
if _drop_env:
    _min_drop_pct = float(_drop_env)
else:
    _min_drop_pct = float(os.getenv("PANIC_FLUSH_MIN_DROP", "-4"))
# If the env uses a fractional style like -0.8, interpret as -8% by scaling.
if _min_drop_pct > -2:
    _min_drop_pct *= 10


@dataclass
class DailyStats:
    prev_close: float
    prev_low: float
    recent_low: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    avg_volume: float

    @property
    def rvol(self) -> float:
        return self.volume / self.avg_volume if self.avg_volume > 0 else 0.0

    @property
    def dollar_vol(self) -> float:
        return self.close * self.volume

    @property
    def day_change_pct(self) -> float:
        return ((self.close - self.prev_close) / self.prev_close * 100) if self.prev_close > 0 else 0.0

    @property
    def from_open_pct(self) -> float:
        return ((self.close - self.open) / self.open * 100) if self.open > 0 else 0.0

    @property
    def low_close_distance_pct(self) -> float:
        return ((self.close - self.low) / self.close * 100) if self.close > 0 else 0.0


def _compute_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    gains: List[float] = []
    losses: List[float] = []
    for prev, curr in zip(closes[:-1], closes[1:]):
        delta = curr - prev
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = mean(gains[-period:]) if gains[-period:] else 0.0
    avg_loss = mean(losses[-period:]) if losses[-period:] else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 0.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _fetch_daily(sym: str, days: int) -> List:
    if not _client:
        return []
    start = (date.today() - timedelta(days=days + 2)).isoformat()
    end = date.today().isoformat()
    try:
        return list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=days + 5,
                sort="asc",
            )
        )
    except Exception as exc:
        print(f"[panic_flush] daily agg error for {sym}: {exc}")
        return []


def _extract_ohlcv(bar: any) -> Tuple[float, float, float, float, float]:
    open_ = float(getattr(bar, "open", getattr(bar, "o", 0.0)) or 0.0)
    high = float(getattr(bar, "high", getattr(bar, "h", 0.0)) or 0.0)
    low = float(getattr(bar, "low", getattr(bar, "l", 0.0)) or 0.0)
    close = float(getattr(bar, "close", getattr(bar, "c", 0.0)) or 0.0)
    volume = float(getattr(bar, "volume", getattr(bar, "v", 0.0)) or 0.0)
    return open_, high, low, close, volume


def _compute_daily_stats(sym: str) -> DailyStats | None:
    daily = _fetch_daily(sym, max(_avg_vol_lookback, 6))
    if len(daily) < 2:
        return None

    today_bar = daily[-1]
    prev_bar = daily[-2]

    today_ts = getattr(today_bar, "timestamp", getattr(today_bar, "t", None))
    if today_ts and today_ts > 1e12:
        today_ts /= 1000.0
    if today_ts:
        dt = datetime.utcfromtimestamp(today_ts)
        if dt.date() != date.today():
            return None

    open_, high, low, close, volume = _extract_ohlcv(today_bar)
    prev_close = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0.0)) or 0.0)
    prev_low = float(getattr(prev_bar, "low", getattr(prev_bar, "l", 0.0)) or 0.0)
    if any(x <= 0 for x in (open_, high, low, close, volume, prev_close)):
        return None

    lows_history = [
        _extract_ohlcv(b)[2]
        for b in daily[:-1][-20:]
        if _extract_ohlcv(b)[2] > 0
    ]
    recent_low = min(lows_history) if lows_history else 0.0

    history_vols = [
        _extract_ohlcv(b)[4]
        for b in daily[:-1][-_avg_vol_lookback:]
        if _extract_ohlcv(b)[4] > 0
    ]
    avg_volume = mean(history_vols) if history_vols else 0.0

    return DailyStats(
        prev_close=prev_close,
        prev_low=prev_low,
        recent_low=recent_low,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        avg_volume=avg_volume,
    )


def _fetch_intraday(sym: str) -> List:
    """Fetch same-day 5m bars filtered to RTH."""
    if not _client:
        return []
    start = date.today().isoformat()
    try:
        bars = list(
            _client.list_aggs(
                sym,
                5,
                "minute",
                start,
                start,
                limit=500,
                sort="asc",
            )
        )
    except Exception as exc:
        print(f"[panic_flush] intraday agg error for {sym}: {exc}")
        return []

    filtered: List = []
    for b in bars:
        ts = getattr(b, "timestamp", getattr(b, "t", None))
        if ts is None:
            continue
        if ts > 1e12:
            ts = ts / 1000.0
        dt = datetime.utcfromtimestamp(ts)
        if dt.date() != date.today():
            continue
        minutes = dt.hour * 60 + dt.minute
        if minutes < 9 * 60 + 30 or minutes > 16 * 60:
            continue
        filtered.append(b)
    return filtered


def _compute_vwap(bars: List) -> float:
    if not bars:
        return 0.0
    cumulative_pv = 0.0
    cumulative_v = 0.0
    for b in bars:
        price = float(getattr(b, "vw", None) or getattr(b, "close", getattr(b, "c", 0.0)) or 0.0)
        vol = float(getattr(b, "volume", getattr(b, "v", 0.0)) or 0.0)
        if price > 0 and vol > 0:
            cumulative_pv += price * vol
            cumulative_v += vol
    return cumulative_pv / cumulative_v if cumulative_v > 0 else 0.0


def _format_relative_vwap(last: float, vwap: float) -> str:
    if last <= 0 or vwap <= 0:
        return "n/a"
    diff_pct = (last - vwap) / vwap * 100
    if diff_pct <= -1.5:
        return f"BELOW ({diff_pct:.1f}%)"
    if diff_pct < 0:
        return f"slightly below VWAP ({diff_pct:.1f}%)"
    if diff_pct < 1.5:
        return f"hugging VWAP ({diff_pct:.1f}%)"
    return f"ABOVE (+{diff_pct:.1f}%)"


def _day_structure(summary: DailyStats, dist_from_low_pct: float, vwap_diff: float) -> str:
    if summary.day_change_pct <= _min_drop_pct * 1.2 and dist_from_low_pct < 1.5 and vwap_diff < -1.5:
        return "heavy intraday selloff, near session lows with capitulation-style volume"
    if dist_from_low_pct < 3 and vwap_diff < 0:
        return "flush off the open, stabilizing slightly above lows with tentative bids"
    return "downtrend day with elevated volume"


def _format_panic_alert(sym: str, stats: DailyStats, intraday: List) -> str:
    vwap = _compute_vwap(intraday)
    dist_from_low_pct = (stats.close - stats.low) / stats.low * 100 if stats.low > 0 else 0.0
    vwap_text = _format_relative_vwap(stats.close, vwap) if vwap else "n/a"
    vwap_diff = ((stats.close - vwap) / vwap * 100) if vwap else 0.0
    structure_text = _day_structure(stats, dist_from_low_pct, vwap_diff)
    recent_low_text = None
    if stats.recent_low > 0:
        if stats.close <= stats.recent_low * 1.02:
            recent_low_text = f"Pressing into recent lows near ${stats.recent_low:.2f}"
        else:
            recent_low_text = "Not near recent lows"

    bounce_high = None
    rsi_val = 0.0
    if intraday:
        lows = [(_extract_ohlcv(b)[2], idx) for idx, b in enumerate(intraday)]
        if lows:
            _, low_idx = min(lows, key=lambda x: x[0])
            highs_after_low = [_extract_ohlcv(b)[1] for b in intraday[low_idx:]]
            bounce_high_val = max(highs_after_low) if highs_after_low else 0.0
            bounce_high = bounce_high_val if bounce_high_val > 0 else None
        closes = [
            float(getattr(b, "close", getattr(b, "c", 0.0)) or 0.0)
            for b in intraday
            if float(getattr(b, "close", getattr(b, "c", 0.0)) or 0.0) > 0
        ]
        rsi_val = _compute_rsi(closes) if closes else 0.0

    timestamp = format_est_timestamp()
    header = f"⚠️ PANIC FLUSH — {sym}"
    lines = [
        header,
        f"🕒 {timestamp}",
        "",
        "💰 Price + Damage",
        f"• Last: ${stats.close:.2f} ({stats.day_change_pct:.1f}% today)",
        f"• O ${stats.open:.2f} · H ${stats.high:.2f} · L ${stats.low:.2f} · C ${stats.close:.2f}",
        f"• Distance from LOD: {dist_from_low_pct:.1f}%",
        "",
        "📊 Liquidity",
        f"• Volume: {int(stats.volume):,}",
        f"• RVOL: {stats.rvol:.1f}×",
        f"• Dollar Vol: ${stats.dollar_vol:,.0f}",
    ]

    lines.append("")
    lines.append("📉 Context")
    if vwap:
        lines.append(f"• VWAP: {vwap_text}")
    if rsi_val:
        lines.append(f"• RSI(14): {rsi_val:.1f} (pressure zone)")
    if recent_low_text:
        lines.append(f"• Multi-day context: {recent_low_text}")
    else:
        lines.append("• Multi-day context: n/a")
    if vwap:
        lines.append(f"• Day structure: {structure_text}")
    if bounce_high:
        lines.append(f"• Bounce high after LOD: ${bounce_high:.2f}")

    lines.append("")
    lines.append("🧠 Read")
    lines.append("Heavy capitulation selling with price pinned near lows. Very risky, but often where reflex bounces can start.")
    lines.append("")
    lines.append("🔗 Chart")
    lines.append(chart_link(sym))
    return "\n".join(lines)


async def run_panic_flush() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not POLYGON_KEY or not _client:
            print("[panic_flush] missing POLYGON_KEY; skipping")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        if not _allow_outside_rth and not in_rth_window_est():
            print("[panic_flush] outside RTH; skipping")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        universe = resolve_universe_for_bot(bot_name=BOT_NAME)
        print(f"[panic_flush] universe_size={len(universe)}")
        if not universe:
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        for sym in universe:
            try:
                stats = _compute_daily_stats(sym)
                if not stats:
                    debug_filter_reason(BOT_NAME, sym, "panic_no_data")
                    reason_counts["no_data"] = reason_counts.get("no_data", 0) + 1
                    continue

                scanned += 1

                if stats.close < _min_price:
                    debug_filter_reason(BOT_NAME, sym, "panic_price_too_low")
                    reason_counts["price"] = reason_counts.get("price", 0) + 1
                    continue

                if stats.day_change_pct > _min_drop_pct:
                    debug_filter_reason(BOT_NAME, sym, "panic_drop_not_big_enough")
                    reason_counts["drop"] = reason_counts.get("drop", 0) + 1
                    continue

                if stats.low_close_distance_pct > _max_from_low_pct:
                    debug_filter_reason(BOT_NAME, sym, "panic_not_near_low")
                    reason_counts["near_low"] = reason_counts.get("near_low", 0) + 1
                    continue

                if stats.dollar_vol < max(MIN_VOLUME_GLOBAL * stats.close, _min_dollar_vol):
                    debug_filter_reason(BOT_NAME, sym, "panic_dollar_vol_too_low")
                    reason_counts["dollar_vol"] = reason_counts.get("dollar_vol", 0) + 1
                    continue

                if stats.rvol < max(_min_rvol, MIN_RVOL_GLOBAL):
                    debug_filter_reason(BOT_NAME, sym, "panic_rvol_too_low")
                    reason_counts["rvol"] = reason_counts.get("rvol", 0) + 1
                    continue

                range_span = stats.high - stats.low
                if range_span > 0:
                    range_pos = (stats.close - stats.low) / range_span
                    if range_pos > 0.2:
                        debug_filter_reason(BOT_NAME, sym, "panic_not_bottom_range")
                        reason_counts["range_pos"] = reason_counts.get("range_pos", 0) + 1
                        continue

                intraday = _fetch_intraday(sym)
                if not intraday:
                    debug_filter_reason(BOT_NAME, sym, "panic_no_intraday")
                    reason_counts["no_intraday"] = reason_counts.get("no_intraday", 0) + 1
                    continue
                vwap = _compute_vwap(intraday)
                if vwap <= 0 or stats.close >= vwap:
                    debug_filter_reason(BOT_NAME, sym, "panic_not_below_vwap")
                    reason_counts["vwap"] = reason_counts.get("vwap", 0) + 1
                    continue

                matches += 1
                alert_text = _format_panic_alert(sym, stats, intraday)
                send_alert_text(alert_text)
                alerts += 1
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[panic_flush] error for {sym}: {exc}")
                record_error(BOT_NAME, exc)
                continue
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
        if DEBUG_FLOW_REASONS and matches == 0:
            print(f"[panic_flush] No alerts. Filter breakdown: {reason_counts}")
