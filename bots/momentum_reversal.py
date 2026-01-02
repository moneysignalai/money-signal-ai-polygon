"""Momentum Reversal bot: detects strong intraday moves that start to reverse.

Two-sided patterns:
- Bullish reversal: sharp early selloff that is being reclaimed intraday.
- Bearish reversal: sharp early rip that is fading intraday.

Universe: top-volume equities (Polygon/Massive) with fallback to TICKER_UNIVERSE.
Runs during RTH unless MOMENTUM_REVERSAL_ALLOW_OUTSIDE_RTH is true.
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
    in_rth_window_est,
    resolve_universe_for_bot,
    send_alert,
)
from bots.status_report import record_bot_stats

BOT_NAME = "momentum_reversal"

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
_allow_outside_rth = os.getenv("MOMENTUM_REVERSAL_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
_min_price = float(os.getenv("MOMO_REV_MIN_PRICE", "3.0"))
_min_dollar_vol = float(os.getenv("MOMO_REV_MIN_DOLLAR_VOL", "150000"))
_min_rvol = float(os.getenv("MOMO_REV_MIN_RVOL", "1.0"))
_min_move_pct = float(os.getenv("MOMO_REV_MIN_MOVE_PCT", "4.0"))
_min_reclaim_pct = float(os.getenv("MOMO_REV_MIN_RECLAIM_PCT", "0.30"))
_max_from_extreme_pct = float(os.getenv("MOMO_REV_MAX_FROM_EXTREME_PCT", "30.0"))
_avg_vol_lookback = int(os.getenv("MOMO_REV_LOOKBACK_DAYS", os.getenv("DYNAMIC_MAX_LOOKBACK_DAYS", "5")))


@dataclass
class DailySnapshot:
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
        print(f"[momentum_reversal] daily agg error for {sym}: {exc}")
        return []


def _extract_ohlcv(bar: any) -> Tuple[float, float, float, float, float]:
    open_ = float(getattr(bar, "open", getattr(bar, "o", 0.0)) or 0.0)
    high = float(getattr(bar, "high", getattr(bar, "h", 0.0)) or 0.0)
    low = float(getattr(bar, "low", getattr(bar, "l", 0.0)) or 0.0)
    close = float(getattr(bar, "close", getattr(bar, "c", 0.0)) or 0.0)
    volume = float(getattr(bar, "volume", getattr(bar, "v", 0.0)) or 0.0)
    return open_, high, low, close, volume


def _compute_daily(sym: str) -> DailySnapshot | None:
    daily = _fetch_daily(sym, max(_avg_vol_lookback, 6))
    if len(daily) < 1:
        return None

    today_bar = daily[-1]
    today_ts = getattr(today_bar, "timestamp", getattr(today_bar, "t", None))
    if today_ts and today_ts > 1e12:
        today_ts /= 1000.0
    if today_ts:
        dt = datetime.utcfromtimestamp(today_ts)
        if dt.date() != date.today():
            return None

    open_, high, low, close, volume = _extract_ohlcv(today_bar)
    if any(x <= 0 for x in (open_, high, low, close, volume)):
        return None

    history_vols = [
        _extract_ohlcv(b)[4]
        for b in daily[:-1][-_avg_vol_lookback:]
        if _extract_ohlcv(b)[4] > 0
    ]
    avg_volume = mean(history_vols) if history_vols else 0.0

    return DailySnapshot(open_, high, low, close, volume, avg_volume)


def _fetch_intraday(sym: str) -> List:
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
        print(f"[momentum_reversal] intraday agg error for {sym}: {exc}")
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


def _compute_reversal(sym: str, daily: DailySnapshot, intraday: List) -> Tuple[bool, bool, float, float, float]:
    if not intraday:
        raise ValueError("no intraday data")

    open_, _, _, _, _ = _extract_ohlcv(intraday[0])
    _, _, _, latest_close, _ = _extract_ohlcv(intraday[-1])
    high_price = max(_extract_ohlcv(b)[1] for b in intraday)
    low_price = min(_extract_ohlcv(b)[2] for b in intraday)

    high_move = (high_price - open_) / open_ * 100 if open_ > 0 else 0.0
    low_move = (low_price - open_) / open_ * 100 if open_ > 0 else 0.0

    # Determine dominant initial move
    bullish_reversal = False
    bearish_reversal = False
    move_pct = 0.0
    reclaim_pct = 0.0

    if abs(low_move) > abs(high_move):
        move_pct = abs(low_move)
        if high_price != low_price:
            reclaim_pct = (latest_close - low_price) / (open_ - low_price) if open_ != low_price else 0.0
        bullish_reversal = reclaim_pct >= _min_reclaim_pct
    else:
        move_pct = abs(high_move)
        if high_price != open_:
            reclaim_pct = (high_price - latest_close) / (high_price - open_)
        bearish_reversal = reclaim_pct >= _min_reclaim_pct

    return bullish_reversal, bearish_reversal, move_pct, reclaim_pct, latest_close


async def run_momentum_reversal() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not POLYGON_KEY or not _client:
            print("[momentum_reversal] missing POLYGON_KEY; skipping")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        if not _allow_outside_rth and not in_rth_window_est():
            print("[momentum_reversal] outside RTH; skipping")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        universe = resolve_universe_for_bot(bot_name=BOT_NAME)
        print(f"[momentum_reversal] universe_size={len(universe)}")
        if not universe:
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
            return

        for sym in universe:
            try:
                daily = _compute_daily(sym)
                if not daily:
                    debug_filter_reason(BOT_NAME, sym, "momo_no_daily_data")
                    reason_counts["daily"] = reason_counts.get("daily", 0) + 1
                    continue

                if daily.close < _min_price:
                    debug_filter_reason(BOT_NAME, sym, "momo_price_too_low")
                    reason_counts["price"] = reason_counts.get("price", 0) + 1
                    continue

                intraday = _fetch_intraday(sym)
                if not intraday:
                    debug_filter_reason(BOT_NAME, sym, "momo_no_intraday")
                    reason_counts["intraday"] = reason_counts.get("intraday", 0) + 1
                    continue

                scanned += 1

                bullish, bearish, move_pct, reclaim_pct, last_price = _compute_reversal(
                    sym, daily, intraday
                )

                if move_pct < _min_move_pct:
                    debug_filter_reason(BOT_NAME, sym, "momo_move_too_small")
                    reason_counts["move"] = reason_counts.get("move", 0) + 1
                    continue

                if move_pct > _max_from_extreme_pct:
                    debug_filter_reason(BOT_NAME, sym, "momo_move_excessive")
                    reason_counts["move_excess"] = reason_counts.get("move_excess", 0) + 1
                    continue

                if reclaim_pct < _min_reclaim_pct:
                    debug_filter_reason(BOT_NAME, sym, "momo_reclaim_too_small")
                    reason_counts["reclaim"] = reason_counts.get("reclaim", 0) + 1
                    continue

                if daily.dollar_vol < max(_min_dollar_vol, MIN_VOLUME_GLOBAL * daily.close):
                    debug_filter_reason(BOT_NAME, sym, "momo_dollar_vol_too_low")
                    reason_counts["dollar_vol"] = reason_counts.get("dollar_vol", 0) + 1
                    continue

                if daily.rvol < max(_min_rvol, MIN_RVOL_GLOBAL):
                    debug_filter_reason(BOT_NAME, sym, "momo_rvol_too_low")
                    reason_counts["rvol"] = reason_counts.get("rvol", 0) + 1
                    continue

                if not (bullish or bearish):
                    debug_filter_reason(BOT_NAME, sym, "momo_no_reversal")
                    reason_counts["no_reversal"] = reason_counts.get("no_reversal", 0) + 1
                    continue

                direction = "Bullish" if bullish else "Bearish"
                matches += 1

                body = (
                    f"• Last: ${last_price:.2f}\n"
                    f"• Initial move: {move_pct:.1f}% from open\n"
                    f"• Reclaim: {reclaim_pct * 100:.1f}% of initial move\n"
                    f"• Volume: {int(daily.volume):,} ({daily.rvol:.1f}× avg) — Dollar Vol: ${daily.dollar_vol:,.0f}\n"
                    f"• Context: Strong intraday reversal ({direction.lower()}).\n"
                    f"• Chart: {chart_link(sym)}"
                )
                send_alert(
                    f"{BOT_NAME} {direction}",
                    sym,
                    last_price,
                    daily.rvol,
                    extra=body,
                )
                alerts += 1
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[momentum_reversal] error for {sym}: {exc}")
                continue
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
        if DEBUG_FLOW_REASONS and matches == 0:
            print(f"[momentum_reversal] No alerts. Filter breakdown: {reason_counts}")
