# bots/openingrangebreakout.py
#
# Opening Range Breakout (ORB) bot — STOCKS ONLY
#
# Upgraded version with:
#   • 15-min ORB (configurable via ORB_RANGE_MINUTES)
#   • Breakout / breakdown detection
#   • Retest logic of ORB high/low (break -> pullback -> go)
#   • Fair Value Gap (FVG) detection to add confluence
#   • RVOL + dollar-volume filters so you don’t get junk
#   • Single long/short alert per symbol per day

import os
import time
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz

try:
    from massive import RESTClient  # optional internal wrapper
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    get_dynamic_top_volume_universe,
    send_alert,
    chart_link,
    now_est,
    today_est_date,
    minutes_since_midnight_est,
    is_etf_blacklisted,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")

_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

ORB_RANGE_MINUTES = int(os.getenv("ORB_RANGE_MINUTES", "15"))

# When do we actually scan for ORB plays?
#   • Start: right after the ORB window finishes
#   • End: late morning by default (you can still change this)
ORB_SCAN_START_MIN = 9 * 60 + 30 + ORB_RANGE_MINUTES  # e.g. 09:45 for 15-min ORB
ORB_SCAN_END_MIN = 12 * 60 + 0  # 12:00 ET

# Price / RVOL / dollar-volume filters
ORB_MIN_PRICE = float(os.getenv("ORB_MIN_PRICE", "5.0"))
ORB_MIN_DOLLAR_VOL = float(os.getenv("ORB_MIN_DOLLAR_VOL", os.getenv("ORB_MIN_DOLLAR_VOL", "200000")))
ORB_MIN_RVOL = float(os.getenv("ORB_MIN_RVOL", "1.0"))

# Universe size
ORB_MAX_UNIVERSE = int(os.getenv("ORB_MAX_UNIVERSE", "120"))

# Retest / buffer configuration
# How close the retest needs to come back to the ORB high/low (as a fraction).
ORB_RETEST_TOLERANCE_PCT = float(os.getenv("ORB_RETEST_TOLERANCE_PCT", "0.0015"))  # 0.15%

# FVG lookback in bars (1-minute bars)
FVG_LOOKBACK_BARS = int(os.getenv("ORB_FVG_LOOKBACK_BARS", "50"))

# Per-day de-dupe so you only get one long/short per symbol per day
_alert_day: Optional[date] = None
_seen_long: set[str] = set()
_seen_short: set[str] = set()


def _reset_day() -> None:
    global _alert_day, _seen_long, _seen_short
    today = today_est_date()
    if _alert_day != today:
        _alert_day = today
        _seen_long = set()
        _seen_short = set()
        print("[opening_range_breakout] New trading day – reset seen sets.")


def _in_orb_window() -> bool:
    mins = minutes_since_midnight_est()
    return ORB_SCAN_START_MIN <= mins <= ORB_SCAN_END_MIN


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _fetch_intraday_1min(sym: str, trading_day: date) -> List[Dict[str, Any]]:
    """
    Fetch 1-minute intraday bars from 09:30 ET to 'now' for the given symbol & trading day.
    """
    if not _client:
        return []

    start = datetime(trading_day.year, trading_day.month, trading_day.day, 9, 30, tzinfo=eastern)
    end = datetime.now(eastern)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    try:
        resp = _client.list_aggs(
            ticker=sym,
            multiplier=1,
            timespan="minute",
            from_=start_ms,
            to=end_ms,
            adjusted=True,
            sort="asc",
            limit=5000,
        )
        bars: List[Dict[str, Any]] = []
        for a in resp:
            d = a.__dict__
            ts = d.get("timestamp") or d.get("t")
            if ts is None:
                continue
            if ts > 1e12:  # ms -> s
                ts = ts / 1000.0
            dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
            dt_et = dt_utc.astimezone(eastern)
            bars.append(
                {
                    "dt": dt_et,
                    "o": _safe_float(d.get("open") or d.get("o")),
                    "h": _safe_float(d.get("high") or d.get("h")),
                    "l": _safe_float(d.get("low") or d.get("l")),
                    "c": _safe_float(d.get("close") or d.get("c")),
                    "v": _safe_float(d.get("volume") or d.get("v")) or 0.0,
                }
            )
        bars = [b for b in bars if b["c"] is not None]
        return bars
    except Exception as e:
        print(f"[opening_range_breakout] error fetching 1-min aggs for {sym}: {e}")
        return []


def _compute_rvol(sym: str, trading_day: date, day_vol: float) -> float:
    """
    Very lightweight 20-day RVOL using daily bars.
    """
    if not _client:
        return 1.0

    try:
        start = (trading_day - timedelta(days=40)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                adjusted=True,
                sort="desc",
                limit=40,
            )
        )
        if not daily:
            return 1.0

        # exclude today's bar from the average
        hist = daily[1:] if len(daily) > 1 else daily
        vols = [_safe_float(getattr(b, "volume", getattr(b, "v", None))) or 0.0 for b in hist]
        vols = [v for v in vols if v > 0]
        if not vols:
            return 1.0

        avg_vol = sum(vols) / len(vols)

        # Adjust for how far we are into the trading day (minutes since 09:30 / 390)
        now_mins = minutes_since_midnight_est()
        minutes_since_open = max(0, now_mins - (9 * 60 + 30))
        intraday_frac = min(1.0, minutes_since_open / 390.0)
        if intraday_frac <= 0:
            return 1.0

        expected_by_now = avg_vol * intraday_frac
        if expected_by_now <= 0:
            return 1.0

        return day_vol / expected_by_now
    except Exception as e:
        print(f"[opening_range_breakout] RVOL error for {sym}: {e}")
        return 1.0


def _find_last_fvg(
    bars: List[Dict[str, Any]],
    direction: str,
    lookback: int,
) -> Optional[Tuple[float, float]]:
    """
    Very simple 3-candle fair value gap (FVG) detection on 1-min bars.

    Bullish FVG (direction='up'):
      low[i] > high[i-2]  -> gap between high[i-2] and low[i]

    Bearish FVG (direction='down'):
      high[i] < low[i-2]  -> gap between high[i] and low[i-2]
    """
    if len(bars) < 3:
        return None

    start = max(2, len(bars) - lookback)
    if direction == "up":
        for i in range(len(bars) - 1, start - 1, -1):
            l0 = bars[i]["l"]
            h2 = bars[i - 2]["h"]
            if l0 is None or h2 is None:
                continue
            if l0 > h2:
                # FVG zone between h2 and l0
                return (h2, l0)
    elif direction == "down":
        for i in range(len(bars) - 1, start - 1, -1):
            h0 = bars[i]["h"]
            l2 = bars[i - 2]["l"]
            if h0 is None or l2 is None:
                continue
            if h0 < l2:
                # FVG zone between h0 and l2
                return (h0, l2)
    return None


def _detect_orb_signals(
    bars: List[Dict[str, Any]],
    orb_start: datetime,
    orb_end: datetime,
) -> Tuple[bool, bool, Optional[Tuple[float, float]], Optional[Tuple[float, float]], Optional[Dict[str, Any]]]:
    """
    Given full 1-min bars for the day and the ORB window, compute:
      • ORB high/low
      • Whether we have a breakout + retest for long / short
      • Most recent bullish/bearish FVG
      • The latest bar used as 'trigger'
    """
    if not bars:
        return False, False, None, None, None

    orb_bars = [b for b in bars if orb_start <= b["dt"] < orb_end]
    post_bars = [b for b in bars if b["dt"] >= orb_end]

    if len(orb_bars) < max(3, int(ORB_RANGE_MINUTES * 0.6)):
        return False, False, None, None, None
    if len(post_bars) < 3:
        return False, False, None, None, None

    orb_high = max(b["h"] for b in orb_bars if b["h"] is not None)
    orb_low = min(b["l"] for b in orb_bars if b["l"] is not None)
    if orb_high is None or orb_low is None:
        return False, False, None, None, None

    tol_up = orb_high * ORB_RETEST_TOLERANCE_PCT
    tol_dn = abs(orb_low) * ORB_RETEST_TOLERANCE_PCT

    broke_up = False
    retested_up = False

    broke_dn = False
    retested_dn = False

    last_bar = post_bars[-1]

    for b in post_bars:
        c = b["c"]
        h = b["h"]
        l = b["l"]
        if c is None or h is None or l is None:
            continue

        # Breakout above ORB high
        if not broke_up and c > orb_high + tol_up:
            broke_up = True
        elif broke_up and not retested_up:
            # Retest zone: price trades back into band around ORB high
            if l <= orb_high + tol_up and h >= orb_high - tol_up:
                retested_up = True

        # Breakdown below ORB low
        if not broke_dn and c < orb_low - tol_dn:
            broke_dn = True
        elif broke_dn and not retested_dn:
            # Retest zone: price trades back into band around ORB low
            if h >= orb_low - tol_dn and l <= orb_low + tol_dn:
                retested_dn = True

    long_trigger = bool(broke_up and retested_up)
    short_trigger = bool(broke_dn and retested_dn)

    # FVGs for context (not required to fire)
    bull_fvg = _find_last_fvg(bars, "up", FVG_LOOKBACK_BARS)
    bear_fvg = _find_last_fvg(bars, "down", FVG_LOOKBACK_BARS)

    # Attach ORB levels to last_bar for convenience
    last_bar["orb_high"] = orb_high
    last_bar["orb_low"] = orb_low

    return long_trigger, short_trigger, bull_fvg, bear_fvg, last_bar


async def run_opening_range_breakout() -> None:
    """
    Opening Range Breakout bot with FVG + retest logic.

    - Defines ORB_RANGE_MINUTES (default 15) from 09:30 ET.
    - Requires:
        • Break above/below ORB high/low
        • Retest of that level
        • RVOL + dollar-volume + price filters
    - Adds FVG context into the alert for extra confluence.
    """
    _reset_day()

    if not POLYGON_KEY or not _client:
        print("[opening_range_breakout] missing POLYGON_KEY or client; skipping.")
        return

    if not _in_orb_window():
        print("[opening_range_breakout] outside ORB scan window; skipping.")
        return

    BOT_NAME = "opening_range_breakout"
    start_ts = time.time()
    alerts_sent = 0
    matched_syms: set[str] = set()

    trading_day = today_est_date()
    now_str = now_est()

    universe = get_dynamic_top_volume_universe(
        max_tickers=ORB_MAX_UNIVERSE,
        volume_coverage=0.90,
    )
    if not universe:
        print("[opening_range_breakout] empty universe; skipping.")
        return

    print(f"[opening_range_breakout] scanning {len(universe)} symbols")

    orb_start = datetime(trading_day.year, trading_day.month, trading_day.day, 9, 30, tzinfo=eastern)
    orb_end = orb_start + timedelta(minutes=ORB_RANGE_MINUTES)

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        bars = _fetch_intraday_1min(sym, trading_day)
        if not bars:
            continue

        # Basic intraday aggregates for filters
        day_vol = sum(b["v"] for b in bars)
        day_dollar_vol = sum((b["c"] or 0.0) * b["v"] for b in bars)
        last_price = bars[-1]["c"]

        if last_price is None or last_price < ORB_MIN_PRICE:
            continue
        if day_vol < MIN_VOLUME_GLOBAL or day_dollar_vol < ORB_MIN_DOLLAR_VOL:
            continue

        rvol = _compute_rvol(sym, trading_day, day_vol)
        if rvol < max(ORB_MIN_RVOL, MIN_RVOL_GLOBAL):
            continue

        long_trigger, short_trigger, bull_fvg, bear_fvg, last_bar = _detect_orb_signals(
            bars, orb_start, orb_end
        )
        if last_bar is None:
            continue

        orb_high = last_bar["orb_high"]
        orb_low = last_bar["orb_low"]

        # LONG: ORB breakout + retest
        if long_trigger and sym not in _seen_long:
            fvg_line = ""
            if bull_fvg:
                f_lo, f_hi = bull_fvg
                if last_bar["l"] <= f_hi and last_bar["h"] >= f_lo:
                    fvg_line = f"🟩 Retest of bullish FVG zone: ${f_lo:.2f}–${f_hi:.2f}"
                else:
                    fvg_line = f"🟩 Bullish FVG below: ${f_lo:.2f}–${f_hi:.2f}"

            header = f"🚀 ORB LONG (breakout + retest) — {sym}"
            body_lines = [
                header,
                f"🕒 {now_str}",
                "────────────",
                f"💰 Price: ${last_price:.2f}",
                f"📊 RVOL: {rvol:.2f}",
                f"💵 Intraday $Vol: ≈ ${day_dollar_vol:,.0f}",
                "",
                f"📈 ORB High: ${orb_high:.2f}",
                f"📉 ORB Low:  ${orb_low:.2f}",
                f"🔁 Retest band: around ${orb_high:.2f}",
            ]
            if fvg_line:
                body_lines.append(fvg_line)
            body_lines.extend(
                [
                    "",
                    "Bias: LONG idea above ORB high after retest. Combine with RSI + options flow for entries.",
                    f"🔗 Chart: {chart_link(sym)}",
                ]
            )
            extra = "\n".join(body_lines)
            send_alert("orb_long", sym, float(last_price), rvol, extra=extra)
            _seen_long.add(sym)
            matched_syms.add(sym)
            alerts_sent += 1
            continue

        # SHORT: ORB breakdown + retest
        if short_trigger and sym not in _seen_short:
            fvg_line = ""
            if bear_fvg:
                f_hi, f_lo = bear_fvg
                if last_bar["l"] <= f_hi and last_bar["h"] >= f_lo:
                    fvg_line = f"🟥 Retest of bearish FVG zone: ${f_lo:.2f}–${f_hi:.2f}"
                else:
                    fvg_line = f"🟥 Bearish FVG above: ${f_lo:.2f}–${f_hi:.2f}"

            header = f"🔻 ORB SHORT (breakdown + retest) — {sym}"
            body_lines = [
                header,
                f"🕒 {now_str}",
                "────────────",
                f"💰 Price: ${last_price:.2f}",
                f"📊 RVOL: {rvol:.2f}",
                f"💵 Intraday $Vol: ≈ ${day_dollar_vol:,.0f}",
                "",
                f"📉 ORB Low:  ${orb_low:.2f}",
                f"📈 ORB High: ${orb_high:.2f}",
                f"🔁 Retest band: around ${orb_low:.2f}",
            ]
            if fvg_line:
                body_lines.append(fvg_line)
            body_lines.extend(
                [
                    "",
                    "Bias: SHORT / take-profit idea below ORB low after retest. Combine with RSI + options flow for timing.",
                    f"🔗 Chart: {chart_link(sym)}",
                ]
            )
            extra = "\n".join(body_lines)
            send_alert("orb_short", sym, float(last_price), rvol, extra=extra)
            _seen_short.add(sym)
            matched_syms.add(sym)
            alerts_sent += 1

    runtime = time.time() - start_ts
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matched_syms),
            alerts=alerts_sent,
            runtime=runtime,
        )
    except Exception as e:
        print(f"[opening_range_breakout] record_bot_stats error: {e}")

    print("[opening_range_breakout] scan complete.")