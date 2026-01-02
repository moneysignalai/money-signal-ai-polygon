# bots/rsi_signals.py
#
# Intraday RSI scanner for overbought / oversold signals with upgraded, human-readable
# alerts. Uses intraday minute aggregates (default 5-min) plus daily history to add
# context on trend, RVOL, and dollar volume. Alerts are sent once per symbol per
# side (overbought/oversold) per trading day.

from __future__ import annotations

import math
import os
import statistics
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

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
    format_est_timestamp,
    in_rth_window_est,
    send_alert_text,
    resolve_universe_for_bot,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "rsi_signals"

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None


# ---------------- CONFIG ----------------

RSI_MIN_PRICE = float(os.getenv("RSI_MIN_PRICE", "5.0"))
RSI_MIN_DOLLAR_VOL = float(os.getenv("RSI_MIN_DOLLAR_VOL", "200000"))
RSI_MAX_UNIVERSE = int(os.getenv("RSI_MAX_UNIVERSE", os.getenv("DYNAMIC_MAX_TICKERS", "2000")))

RSI_TIMEFRAME_MIN = int(os.getenv("RSI_TIMEFRAME_MIN", "5"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30.0"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70.0"))

RSI_LOOKBACK_DAYS = int(os.getenv("RSI_LOOKBACK_DAYS", "50"))
_allow_outside_rth = os.getenv("RSI_ALLOW_OUTSIDE_RTH", "false").lower() == "true"


# ---------------- Helpers ----------------


def _fmt_price(val: float) -> str:
    return f"${val:,.2f}" if val > 0 else "N/A"


def _fmt_pct(val: float) -> str:
    return f"{val:+.1f}%"


def _fetch_daily(sym: str, days: int) -> List[Any]:
    """Return daily aggregates sorted asc (oldest → newest)."""

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
    except Exception as exc:  # pragma: no cover - network/REST issues
        print(f"[rsi_signals] daily agg error for {sym}: {exc}")
        return []


def _fetch_intraday(sym: str, minutes: int) -> List[Dict[str, Any]]:
    if not _client:
        return []

    start = date.today().isoformat()
    end = date.today().isoformat()
    try:
        bars = list(
            _client.list_aggs(
                sym,
                minutes,
                "minute",
                start,
                end,
                limit=5000,
                sort="asc",
            )
        )
    except Exception as exc:  # pragma: no cover
        print(f"[rsi_signals] intraday agg error for {sym}: {exc}")
        return []

    out: List[Dict[str, Any]] = []
    for b in bars:
        out.append(
            {
                "t": getattr(b, "timestamp", getattr(b, "t", None)),
                "o": float(getattr(b, "open", getattr(b, "o", 0.0)) or 0.0),
                "h": float(getattr(b, "high", getattr(b, "h", 0.0)) or 0.0),
                "l": float(getattr(b, "low", getattr(b, "l", 0.0)) or 0.0),
                "c": float(getattr(b, "close", getattr(b, "c", 0.0)) or 0.0),
                "v": float(getattr(b, "volume", getattr(b, "v", 0.0)) or 0.0),
            }
        )
    return out


def _compute_rsi(closes: List[float], period: int) -> List[float]:
    if len(closes) < period + 1:
        return []

    rsis: List[float] = []
    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    rsis.extend([math.nan] * period)
    rsis.append(rsi)

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
        rsis.append(rsi)

    return rsis


def _calc_rvol(day_vol: float, history: List[Any]) -> float:
    volumes = []
    for bar in history:
        vol = float(getattr(bar, "volume", getattr(bar, "v", 0.0)) or 0.0)
        if vol > 0:
            volumes.append(vol)
    avg = statistics.mean(volumes) if volumes else 0.0
    return day_vol / avg if avg > 0 else 0.0


def _regime(price: float, ma20: float, ma50: float) -> str:
    if price > ma20 > ma50:
        return "Uptrend (price > MA20 > MA50)"
    if price < ma20 < ma50 and ma20 > 0 and ma50 > 0:
        return "Downtrend (price < MA20 < MA50)"
    return "Range-bound / mixed MAs"


def _format_rsi_alert(
    symbol: str,
    rsi_val: float,
    last: float,
    open_: float,
    high: float,
    low: float,
    close: float,
    rvol: float,
    volume: float,
    dollar_vol: float,
    day_move_pct: float,
    direction_label: str,
    signal: str,
    distance_from_low_pct: float,
    distance_from_high_pct: float,
    ts: datetime,
) -> str:
    """Return a premium, emoji-rich alert for RSI overbought/oversold events."""

    timestamp = format_est_timestamp(ts)
    header_emoji = "🧠" if signal == "oversold" else "🔥"
    header_title = "RSI OVERSOLD" if signal == "oversold" else "RSI OVERBOUGHT"

    rvol_text = f"{rvol:.1f}×" if rvol > 0 else "N/A"
    vol_text = f"{volume:,.0f}" if volume > 0 else "N/A"
    dollar_vol_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"

    distance_line = (
        f"• Distance from Low: {distance_from_low_pct:.1f}%"
        if signal == "oversold"
        else f"• Distance from High: {distance_from_high_pct:.1f}%"
    )

    rsi_threshold_text = (
        f"(≤ {RSI_OVERSOLD} OVERSOLD)"
        if signal == "oversold"
        else f"(≥ {RSI_OVERBOUGHT} OVERBOUGHT)"
    )

    read_line = (
        "Short-term momentum washed out. Potential bounce or mean-reversion zone."
        if signal == "oversold"
        else "Short-term move looks stretched. Possible fade / digestion zone."
    )

    lines = [
        f"{header_emoji} {header_title} — {symbol}",
        f"🕒 {timestamp}",
        "",
        "💰 Price Snapshot",
        f"• Last: {_fmt_price(last)} ({day_move_pct:.1f}% {direction_label})",
        f"• RVOL: {rvol_text}",
        f"• Dollar Vol: {dollar_vol_text}",
        "",
        "📉 Momentum Setup" if signal == "oversold" else "📈 Momentum Setup",
        f"• RSI({RSI_PERIOD}, {RSI_TIMEFRAME_MIN}m): {rsi_val:.1f} {rsi_threshold_text}",
        f"• Today’s range: O {_fmt_price(open_)} · H {_fmt_price(high)} · L {_fmt_price(low)} · C {_fmt_price(close)}",
        distance_line,
        "",
        "🧠 Read",
        read_line,
        "",
        "🔗 Chart",
        chart_link(symbol),
    ]
    return "\n".join(lines)


def _safe_float(val: Any) -> float:
    try:
        return float(val)
    except Exception:
        return 0.0


def _extract_daily_fields(bar: Any) -> Tuple[float, float, float, float, float]:
    return (
        _safe_float(getattr(bar, "open", getattr(bar, "o", 0.0))),
        _safe_float(getattr(bar, "high", getattr(bar, "h", 0.0))),
        _safe_float(getattr(bar, "low", getattr(bar, "l", 0.0))),
        _safe_float(getattr(bar, "close", getattr(bar, "c", 0.0))),
        _safe_float(getattr(bar, "volume", getattr(bar, "v", 0.0))),
    )


# ---------------- MAIN BOT ----------------


async def run_rsi_signals() -> None:
    if not POLYGON_KEY or not _client:
        print("[rsi_signals] POLYGON_KEY missing; skipping.")
        return

    start_ts = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not _allow_outside_rth and not in_rth_window_est():
            print("[rsi_signals] outside RTH window; skipping run")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start_ts)
            return

        universe = resolve_universe_for_bot(
            bot_name=BOT_NAME,
            bot_env_var="RSI_TICKER_UNIVERSE",
            max_universe_env="RSI_MAX_UNIVERSE",
            default_max_universe=RSI_MAX_UNIVERSE,
        )
        if not universe:
            print("[rsi_signals] universe empty; skipping")
            record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start_ts)
            return

        print(f"[rsi_signals] scanning {len(universe)} symbols")

        for sym in universe:
            scanned += 1
            try:
                daily = _fetch_daily(sym, max(RSI_LOOKBACK_DAYS, 50))
                if len(daily) < 2:
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "insufficient_daily_history")
                    reason_counts["insufficient_daily_history"] = reason_counts.get(
                        "insufficient_daily_history", 0
                    ) + 1
                    continue

                intraday = _fetch_intraday(sym, RSI_TIMEFRAME_MIN)
                if len(intraday) < RSI_PERIOD + 5:
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "insufficient_intraday")
                    reason_counts["insufficient_intraday"] = reason_counts.get(
                        "insufficient_intraday", 0
                    ) + 1
                    continue

                closes = [b["c"] for b in intraday if b.get("c") is not None]
                vols = [b["v"] for b in intraday if b.get("v") is not None]
                if len(closes) < RSI_PERIOD + 5 or len(closes) != len(vols):
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "bad_intraday_series")
                    reason_counts["bad_intraday_series"] = reason_counts.get("bad_intraday_series", 0) + 1
                    continue

                open_, high, low, last = intraday[0]["o"], max(b["h"] for b in intraday), min(b["l"] for b in intraday), closes[-1]
                day_vol = sum(vols)
                dollar_vol = last * day_vol

                if last < RSI_MIN_PRICE:
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "price_below_min")
                    reason_counts["price_below_min"] = reason_counts.get("price_below_min", 0) + 1
                    continue

                if dollar_vol < max(RSI_MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL):
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "dollar_vol_too_low")
                    reason_counts["dollar_vol_too_low"] = reason_counts.get("dollar_vol_too_low", 0) + 1
                    continue

                history = daily[:-1]
                rvol = _calc_rvol(day_vol, history[-20:])
                if rvol < max(MIN_RVOL_GLOBAL, 0.0):
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "rvol_below_floor")
                    reason_counts["rvol_below_floor"] = reason_counts.get("rvol_below_floor", 0) + 1
                    continue

                rsis = _compute_rsi(closes, RSI_PERIOD)
                if len(rsis) != len(closes):
                    continue
                rsi_last = rsis[-1]
                if math.isnan(rsi_last):
                    continue

                prev_close = _extract_daily_fields(daily[-2])[3]
                day_move_pct = ((last - prev_close) / prev_close * 100) if prev_close > 0 else 0.0

                signal: Optional[str] = None
                if rsi_last <= RSI_OVERSOLD:
                    signal = "oversold"
                elif rsi_last >= RSI_OVERBOUGHT:
                    signal = "overbought"

                if not signal:
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "rsi_neutral")
                    reason_counts["rsi_neutral"] = reason_counts.get("rsi_neutral", 0) + 1
                    continue

                direction_label = "UP" if day_move_pct >= 0 else "DOWN"
                distance_from_low_pct = ((last - low) / low * 100) if low > 0 else 0.0
                distance_from_high_pct = ((high - last) / high * 100) if high > 0 else 0.0

                alert_text = _format_rsi_alert(
                    symbol=sym,
                    rsi_val=rsi_last,
                    last=last,
                    open_=open_,
                    high=high,
                    low=low,
                    close=last,
                    rvol=rvol,
                    volume=day_vol,
                    dollar_vol=dollar_vol,
                    day_move_pct=day_move_pct,
                    direction_label=direction_label,
                    signal=signal,
                    distance_from_low_pct=distance_from_low_pct,
                    distance_from_high_pct=distance_from_high_pct,
                    ts=datetime.now(),
                )
                send_alert_text(alert_text)
                matches += 1
                alerts += 1
            except Exception as exc:  # pragma: no cover - per-symbol resilience
                print(f"[rsi_signals] error processing {sym}: {exc}")
                record_error(BOT_NAME, exc)
                continue

        if matches == 0 and DEBUG_FLOW_REASONS:
            print(f"[rsi_signals] No alerts. Filter breakdown: {reason_counts}")
    except Exception as exc:
        print(f"[rsi_signals] runtime error: {exc}")
        record_error(BOT_NAME, exc)
    finally:
        runtime = time.perf_counter() - start_ts
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)


if __name__ == "__main__":  # simple formatter demo
    demo = _format_rsi_alert(
        symbol="AMD",
        rsi_val=28.4,
        last=102.4,
        open_=105.1,
        high=106.3,
        low=100.8,
        close=102.4,
        rvol=1.7,
        volume=8_220_000,
        dollar_vol=842_000_000,
        day_move_pct=-4.6,
        direction_label="DOWN",
        signal="oversold",
        distance_from_low_pct=1.6,
        distance_from_high_pct=3.7,
        ts=datetime(2026, 1, 1, 10, 32),
    )
    print(demo)
