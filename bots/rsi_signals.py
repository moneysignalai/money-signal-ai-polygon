# bots/rsi_signals.py
#
# RSI-based intraday signals:
#   • Oversold bounce candidates (RSI < RSI_OVERSOLD, turning up)
#   • Overbought fade / take-profit candidates (RSI > RSI_OVERBOUGHT, turning down)
#
# Uses intraday 5-min candles for RSI plus basic price/volume filters.

import os
import time
from datetime import datetime, date
from typing import Any, Dict, List

import pytz
from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    resolve_universe_for_bot,
    is_etf_blacklisted,
    minutes_since_midnight_est,
    send_alert,
    chart_link,
    now_est,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

RSI_MIN_PRICE = float(os.getenv("RSI_MIN_PRICE", "5.0"))
RSI_MIN_DOLLAR_VOL = float(os.getenv("RSI_MIN_DOLLAR_VOL", "200000"))
DEFAULT_MAX_UNIVERSE = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
RSI_MAX_UNIVERSE = int(os.getenv("RSI_MAX_UNIVERSE", str(DEFAULT_MAX_UNIVERSE)))

RSI_TIMEFRAME_MIN = int(os.getenv("RSI_TIMEFRAME_MIN", "5"))  # 5-min candles
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30.0"))
RSI_OVERBOUGHT = float(os.getenv("RSI_OVERBOUGHT", "70.0"))

INTRADAY_START_MIN = 9 * 60 + 35  # 09:35 (after first few candles)
INTRADAY_END_MIN = 16 * 60       # 16:00

# Per-day de-dupe
_alert_date: date | None = None
_seen_oversold: set[str] = set()
_seen_overbought: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _seen_oversold, _seen_overbought
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _seen_oversold = set()
        _seen_overbought = set()
        print("[rsi_signals] New trading day – reset seen sets.")


def _in_intraday_window() -> bool:
    mins = minutes_since_midnight_est()
    return INTRADAY_START_MIN <= mins <= INTRADAY_END_MIN


def _fetch_intraday_bars(sym: str, minutes: int) -> List[Dict[str, Any]]:
    """
    Fetch intraday aggregate bars for today for the given minute timeframe.
    Uses Polygon's v2 aggregates endpoint via RESTClient.
    """
    if not _client:
        return []

    today = datetime.now(eastern).date()
    start = datetime(today.year, today.month, today.day, 9, 30, tzinfo=eastern)
    end = datetime.now(eastern)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    try:
        resp = _client.list_aggs(
            ticker=sym,
            multiplier=minutes,
            timespan="minute",
            from_=start_ms,
            to=end_ms,
            adjusted=True,
            sort="asc",
            limit=500,
        )
        bars = [a.__dict__ for a in resp]
        out: List[Dict[str, Any]] = []
        for b in bars:
            out.append(
                {
                    "t": b.get("timestamp") or b.get("t"),
                    "o": b.get("open") or b.get("o"),
                    "h": b.get("high") or b.get("h"),
                    "l": b.get("low") or b.get("l"),
                    "c": b.get("close") or b.get("c"),
                    "v": b.get("volume") or b.get("v"),
                    "vw": b.get("vwap") or b.get("vw"),
                }
            )
        return out
    except Exception as e:
        print(f"[rsi_signals] error fetching aggs for {sym}: {e}")
        return []


def _compute_rsi(closes: List[float], period: int) -> List[float]:
    """
    Simple RSI implementation aligned to closes.
    First `period` values are NaN (no RSI yet).
    """
    import math

    if len(closes) < period + 1:
        return []

    rsis: List[float] = []
    gains: List[float] = []
    losses: List[float] = []

    # First period
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    for _ in range(period):
        rsis.append(math.nan)
    rsis.append(rsi)

    # Subsequent
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


async def run_rsi_signals():
    """
    Scan dynamic universe for intraday RSI extremes and emit:
      • 🟢 RSI OVERSOLD — potential bounce/entry
      • 🔴 RSI OVERBOUGHT — potential fade/take-profit/short
    One alert per symbol per side per day.
    """
    if not POLYGON_KEY:
        print("[rsi_signals] POLYGON_KEY missing; skipping.")
        return
    if not _client:
        print("[rsi_signals] Polygon REST client not initialized; skipping.")
        return
    if not _in_intraday_window():
        print("[rsi_signals] outside intraday window; skipping.")
        return

    _reset_if_new_day()

    BOT_NAME = "rsi_signals"
    start_ts = time.time()
    alerts_sent = 0
    matched_syms: set[str] = set()

    universe = resolve_universe_for_bot(
        bot_name="rsi_signals",
        bot_env_var="RSI_TICKER_UNIVERSE",
        max_universe_env="RSI_MAX_UNIVERSE",
        default_max_universe=DEFAULT_MAX_UNIVERSE,
        apply_dynamic_filters=True,
        volume_coverage_env="DYNAMIC_VOLUME_COVERAGE",
    )
    if not universe:
        print("[rsi_signals] empty universe; skipping.")
        return

    print(f"[rsi_signals] scanning {len(universe)} symbols")

    now_str = now_est()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        bars = _fetch_intraday_bars(sym, RSI_TIMEFRAME_MIN)
        if not bars or len(bars) < RSI_PERIOD + 5:
            continue

        closes = [float(b["c"]) for b in bars if b.get("c") is not None]
        vols = [float(b["v"]) for b in bars if b.get("v") is not None]

        if len(closes) < RSI_PERIOD + 5 or len(vols) != len(closes):
            continue

        last_close = closes[-1]
        total_dollar_vol = sum(closes[i] * vols[i] for i in range(len(closes)))
        if last_close < RSI_MIN_PRICE or total_dollar_vol < RSI_MIN_DOLLAR_VOL:
            continue

        rsis = _compute_rsi(closes, RSI_PERIOD)
        if not rsis or len(rsis) != len(closes):
            continue

        rsi_last = rsis[-1]
        rsi_prev = rsis[-2]

        # filter out NaNs
        if rsi_last != rsi_last or rsi_prev != rsi_prev:
            continue

        # 🟢 Oversold bounce
        if (
            rsi_last <= RSI_OVERSOLD
            and rsi_last > rsi_prev  # turning up
            and sym not in _seen_oversold
        ):
            header = f"🟢 RSI OVERSOLD — {sym}"
            body_lines = [
                header,
                f"🕒 {now_str}",
                "────────────",
                f"📊 RSI: {rsi_last:.1f} (prev {rsi_prev:.1f})",
                f"💰 Price: ${last_close:.2f}",
                f"💵 Intraday dollar volume (approx): ${total_dollar_vol:,.0f}",
                f"⏱ Timeframe: {RSI_TIMEFRAME_MIN}-min",
                f"🔗 Chart: {chart_link(sym)}",
                "",
                "Potential oversold bounce / entry candidate. Combine with ORB, support, and options flow before acting.",
            ]
            extra = "\n".join(body_lines)
            send_alert("rsi_oversold", sym, last_close, 0.0, extra=extra)
            _seen_oversold.add(sym)
            matched_syms.add(sym)
            alerts_sent += 1
            continue

        # 🔴 Overbought fade
        if (
            rsi_last >= RSI_OVERBOUGHT
            and rsi_last < rsi_prev  # turning down
            and sym not in _seen_overbought
        ):
            header = f"🔴 RSI OVERBOUGHT — {sym}"
            body_lines = [
                header,
                f"🕒 {now_str}",
                "────────────",
                f"📊 RSI: {rsi_last:.1f} (prev {rsi_prev:.1f})",
                f"💰 Price: ${last_close:.2f}",
                f"💵 Intraday dollar volume (approx): ${total_dollar_vol:,.0f}",
                f"⏱ Timeframe: {RSI_TIMEFRAME_MIN}-min",
                f"🔗 Chart: {chart_link(sym)}",
                "",
                "Potential overbought fade / take-profit / short candidate. Combine with ORB, resistance, and options flow.",
            ]
            extra = "\n".join(body_lines)
            send_alert("rsi_overbought", sym, last_close, 0.0, extra=extra)
            _seen_overbought.add(sym)
            matched_syms.add(sym)
            alerts_sent += 1

    run_seconds = time.time() - start_ts
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matched_syms),
            alerts=alerts_sent,
            runtime_seconds=run_seconds,
        )
    except Exception as e:
        print(f"[rsi_signals] record_bot_stats error: {e}")

    print("[rsi_signals] scan complete.")