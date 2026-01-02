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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz

try:
    from massive import RESTClient  # optional internal wrapper
except ImportError:  # pragma: no cover - fallback
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    chart_link,
    send_alert_text,
    format_est_timestamp,
    in_rth_window_est,
    is_etf_blacklisted,
    minutes_since_midnight_est,
    now_est_dt,
    resolve_universe_for_bot,
    today_est_date,
)
from bots.status_report import record_bot_stats, record_error

eastern = pytz.timezone("US/Eastern")
BOT_NAME = "opening_range_breakout"


def should_run_now() -> tuple[bool, Optional[str]]:
    """Expose RTH + opening-window gating to the scheduler."""

    allow_outside = os.getenv("ORB_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
    if allow_outside:
        return True, None

    # Only proceed during RTH and within the configured ORB scan window.
    if not in_rth_window_est():
        return False, "outside RTH window"

    if _in_orb_window():
        return True, None
    return False, "outside ORB scan window"


def should_run_now() -> tuple[bool, str | None]:
    """Expose RTH + opening-window gating to the scheduler."""

    allow_outside = os.getenv("ORB_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
    if allow_outside:
        return True, None

    if not in_rth_window_est():
        return False, "outside RTH window"

    try:
        window_minutes = int(os.getenv("ORB_RANGE_MINUTES", "15"))
    except Exception:
        window_minutes = 15

    if in_rth_window_est(0, window_minutes):
        return True, None
    return False, "outside ORB opening window"

_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

ORB_RANGE_MINUTES = int(os.getenv("ORB_RANGE_MINUTES", "15"))

# When do we actually scan for ORB plays?
#   • Start: right after the ORB window finishes (default 5m after open)
#   • End: typically within the first hour (default 60m after open)
_ORB_START_MIN = int(os.getenv("ORB_START_MINUTE", "5"))
_ORB_END_MIN = int(os.getenv("ORB_END_MINUTE", "60"))

# Price / RVOL / dollar-volume filters
ORB_MIN_PRICE = float(os.getenv("ORB_MIN_PRICE", "5.0"))
ORB_MIN_DOLLAR_VOL = float(os.getenv("ORB_MIN_DOLLAR_VOL", "200000"))
ORB_MIN_RVOL = float(os.getenv("ORB_MIN_RVOL", "1.0"))

# Universe size
ORB_MAX_UNIVERSE = int(os.getenv("ORB_MAX_UNIVERSE", "1500"))

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
    open_min = 9 * 60 + 30
    start_window = open_min + max(_ORB_START_MIN, ORB_RANGE_MINUTES)
    end_window = open_min + max(_ORB_END_MIN, start_window - open_min)
    return start_window <= mins <= end_window


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


def _compute_rvol(sym: str, trading_day: date, day_vol: float) -> Tuple[float, Optional[float], Optional[float]]:
    """Compute a lightweight RVOL plus prior-day close/low.

    Returns (rvol, prior_close, prior_low).
    """
    if not _client:
        return 1.0, None, None

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
            return 1.0, None, None

        prior_close = None
        prior_low = None
        if len(daily) > 1:
            prior_bar = daily[1]
            prior_close = _safe_float(getattr(prior_bar, "close", getattr(prior_bar, "c", None)))
            prior_low = _safe_float(getattr(prior_bar, "low", getattr(prior_bar, "l", None)))

        # exclude today's bar from the average
        hist = daily[1:] if len(daily) > 1 else daily
        vols = [_safe_float(getattr(b, "volume", getattr(b, "v", None))) or 0.0 for b in hist]
        vols = [v for v in vols if v > 0]
        if not vols:
            return 1.0, prior_close, prior_low

        avg_vol = sum(vols) / len(vols)

        # Adjust for how far we are into the trading day (minutes since 09:30 / 390)
        now_mins = minutes_since_midnight_est()
        minutes_since_open = max(0, now_mins - (9 * 60 + 30))
        intraday_frac = min(1.0, minutes_since_open / 390.0)
        if intraday_frac <= 0:
            return 1.0

        expected_by_now = avg_vol * intraday_frac
        if expected_by_now <= 0:
            return 1.0, prior_close, prior_low

        return day_vol / expected_by_now, prior_close, prior_low
    except Exception as e:
        print(f"[opening_range_breakout] RVOL error for {sym}: {e}")
        return 1.0, None, None


def _compute_vwap(bars: List[Dict[str, Any]]) -> Optional[float]:
    vol_sum = 0.0
    px_vol_sum = 0.0
    for b in bars:
        price = b.get("c")
        vol = b.get("v") or 0.0
        if price is None or vol <= 0:
            continue
        vol_sum += vol
        px_vol_sum += price * vol
    if vol_sum <= 0:
        return None
    return px_vol_sum / vol_sum


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
    """Opening Range Breakout scanner with retest + FVG context."""

    print("[opening_range_breakout] start")
    _reset_day()

    started_at = now_est_dt()
    start_ts = time.time()
    alerts_sent = 0
    matched_syms: set[str] = set()
    scanned = 0

    if not POLYGON_KEY or not _client:
        print("[opening_range_breakout] missing POLYGON_KEY or client; skipping.")
        finished = now_est_dt()
        record_bot_stats(
            BOT_NAME,
            0,
            0,
            0,
            runtime_seconds=0.0,
            started_at=started_at,
            finished_at=finished,
        )
        return

    if not _in_orb_window():
        print("[opening_range_breakout] outside ORB scan window; skipping.")
        finished = now_est_dt()
        record_bot_stats(
            BOT_NAME,
            0,
            0,
            0,
            runtime_seconds=0.0,
            started_at=started_at,
            finished_at=finished,
        )
        return

    trading_day = today_est_date()
    orb_start = datetime(trading_day.year, trading_day.month, trading_day.day, 9, 30, tzinfo=eastern)
    orb_end = orb_start + timedelta(minutes=ORB_RANGE_MINUTES)

    try:
        universe = resolve_universe_for_bot(
            bot_name=BOT_NAME,
            max_universe_env="ORB_MAX_UNIVERSE",
            default_max_universe=1500,
        )
    except Exception as exc:  # pragma: no cover - defensive
        record_error(BOT_NAME, exc)
        universe = []

    if not universe:
        finished = now_est_dt()
        record_bot_stats(
            BOT_NAME,
            0,
            0,
            0,
            runtime_seconds=time.time() - start_ts,
            started_at=started_at,
            finished_at=finished,
        )
        return

    print(f"[opening_range_breakout] scanning {len(universe)} symbols")

    for sym in universe:
        try:
            if is_etf_blacklisted(sym):
                continue

            bars = _fetch_intraday_1min(sym, trading_day)
            if not bars:
                continue

            scanned += 1

            day_vol = sum(b["v"] for b in bars)
            day_dollar_vol = sum((b["c"] or 0.0) * b["v"] for b in bars)
            last_price = bars[-1]["c"]
            open_price = bars[0]["o"] if bars else None
            session_high = max(b["h"] for b in bars if b["h"] is not None)
            session_low = min(b["l"] for b in bars if b["l"] is not None)

            if last_price is None or last_price < ORB_MIN_PRICE:
                continue
            if day_vol < max(MIN_VOLUME_GLOBAL, 1):
                continue
            if day_dollar_vol < ORB_MIN_DOLLAR_VOL:
                continue

            rvol, prior_close, prior_low = _compute_rvol(sym, trading_day, day_vol)
            if rvol < max(ORB_MIN_RVOL, MIN_RVOL_GLOBAL):
                continue

            long_trigger, short_trigger, bull_fvg, bear_fvg, last_bar = _detect_orb_signals(
                bars, orb_start, orb_end
            )
            if last_bar is None:
                continue

            orb_high = last_bar["orb_high"]
            orb_low = last_bar["orb_low"]
            vwap = _compute_vwap(bars)

            day_move_pct = None
            if prior_close and prior_close > 0:
                day_move_pct = (last_price - prior_close) / prior_close * 100.0
            open_move_pct = None
            if open_price and open_price > 0:
                open_move_pct = (last_price - open_price) / open_price * 100.0
            dist_low_pct = None
            if session_low and session_low > 0:
                dist_low_pct = (last_price - session_low) / session_low * 100.0
            hod_dist_pct = None
            if session_high and session_high > 0:
                hod_dist_pct = (session_high - last_price) / session_high * 100.0

            def _vwap_relation() -> str:
                if vwap is None or vwap <= 0:
                    return "n/a"
                if last_price > vwap * 1.001:
                    return "trading ABOVE VWAP"
                if last_price < vwap * 0.999:
                    return "trading BELOW VWAP"
                return "hugging VWAP"

            def _context_line(direction: str) -> str:
                if direction == "long":
                    if rvol >= 2.0:
                        return "Strong OR breakout with confirmed volume & trend strength"
                    return "Breakout attempt with constructive volume"
                if rvol >= 2.0:
                    return "Aggressive downside pressure + heavy volume breakdown"
                return "Breakdown attempt with elevated supply"

            def _support_level() -> Optional[float]:
                candidates = [x for x in [prior_low, orb_low, session_low] if x]
                if not candidates:
                    return None
                return sorted(candidates, key=lambda x: abs(x - last_price))[0]

            def _resistance_level() -> Optional[float]:
                candidates = [x for x in [session_high, orb_high] if x]
                return max(candidates) if candidates else None

            timestamp = format_est_timestamp()

            # LONG: ORB breakout + retest
            if long_trigger and sym not in _seen_long:
                break_dist = ((last_price - orb_high) / orb_high * 100.0) if orb_high else None
                header = [
                    f"⚡️ OPENING RANGE BREAKOUT — {sym}",
                    f"🕒 {timestamp}",
                ]
                lines = header + [
                    "────────────",
                    "🚀 LONG Breakout Above Opening Range High",
                ]
                price_line = f"💰 Last: ${last_price:.2f}"
                move_bits: List[str] = []
                if day_move_pct is not None:
                    move_bits.append(f"{day_move_pct:+.1f}% vs prior close")
                if open_move_pct is not None:
                    move_bits.append(f"{open_move_pct:+.1f}% from open")
                if hod_dist_pct is not None:
                    move_bits.append(f"{hod_dist_pct:.1f}% below HOD")
                if move_bits:
                    price_line += f" ({', '.join(move_bits)})"
                lines.append(price_line)
                lines.extend(
                    [
                        "",
                        f"📊 Opening Range (first {ORB_RANGE_MINUTES}m)",
                        f"• High: ${orb_high:.2f}",
                        f"• Low: ${orb_low:.2f}",
                        "",
                    ]
                )
                if break_dist is not None:
                    lines.append(f"🔥 Break Distance: {break_dist:+.1f}% above OR high")
                lines.extend(
                    [
                        "",
                        "📈 Volume & Strength",
                        f"• Volume: {day_vol:,.0f} ({rvol:.1f}× avg)",
                        f"• Dollar Vol ≈ ${day_dollar_vol:,.0f}",
                        f"• RVOL: {rvol:.1f}×",
                    ]
                )
                if vwap:
                    lines.append(f"• VWAP: ${vwap:.2f} ({_vwap_relation()})")
                lines.extend(
                    [
                        "",
                        "🔎 Context",
                        _context_line("long"),
                        "",
                        "• Reference levels:",
                    ]
                )
                support = _support_level()
                resistance = _resistance_level()
                if support:
                    lines.append(f"  - Support zone (near-term): ${support:.2f}")
                if resistance:
                    lines.append(f"  - Resistance zone: ${resistance:.2f}")
                lines.extend(
                    [
                        "",
                        "🔗 Chart:",
                        chart_link(sym),
                    ]
                )

                if bull_fvg:
                    f_lo, f_hi = bull_fvg
                    lines.insert(
                        7,
                        f"🟩 Bullish FVG zone: ${f_lo:.2f}–${f_hi:.2f}",
                    )

                send_alert_text("\n".join(lines))
                _seen_long.add(sym)
                matched_syms.add(sym)
                alerts_sent += 1
                continue

            # SHORT: ORB breakdown + retest
            if short_trigger and sym not in _seen_short:
                break_dist = ((last_price - orb_low) / orb_low * 100.0) if orb_low else None
                header = [
                    f"⚡️ OPENING RANGE BREAKDOWN — {sym}",
                    f"🕒 {timestamp}",
                ]
                lines = header + [
                    "────────────",
                    "🩸 SHORT Breakdown Below Opening Range Low",
                ]
                price_line = f"💰 Last: ${last_price:.2f}"
                move_bits: List[str] = []
                if day_move_pct is not None:
                    move_bits.append(f"{day_move_pct:+.1f}% vs prior close")
                if open_move_pct is not None:
                    move_bits.append(f"{open_move_pct:+.1f}% from open")
                if hod_dist_pct is not None:
                    move_bits.append(f"{hod_dist_pct:.1f}% below HOD")
                if move_bits:
                    price_line += f" ({', '.join(move_bits)})"
                lines.append(price_line)
                lines.extend(
                    [
                        "",
                        f"📊 Opening Range (first {ORB_RANGE_MINUTES}m)",
                        f"• High: ${orb_high:.2f}",
                        f"• Low: ${orb_low:.2f}",
                        "",
                    ]
                )
                if break_dist is not None:
                    lines.append(f"🔥 Break Distance: {break_dist:+.1f}% below OR low")
                lines.extend(
                    [
                        "",
                        "📈 Volume & Strength",
                        f"• Volume: {day_vol:,.0f} ({rvol:.1f}× avg)",
                        f"• Dollar Vol ≈ ${day_dollar_vol:,.0f}",
                        f"• RVOL: {rvol:.1f}×",
                    ]
                )
                if vwap:
                    lines.append(f"• VWAP: ${vwap:.2f} ({_vwap_relation()})")
                lines.extend(
                    [
                        "",
                        "🔎 Context",
                        _context_line("short"),
                        "",
                        "• Reference levels:",
                    ]
                )
                support = _support_level()
                resistance = _resistance_level()
                if support:
                    lines.append(f"  - Support zone (near-term): ${support:.2f}")
                if resistance:
                    lines.append(f"  - Resistance zone: ${resistance:.2f}")
                lines.extend(
                    [
                        "",
                        "🔗 Chart:",
                        chart_link(sym),
                    ]
                )

                if bear_fvg:
                    f_hi, f_lo = bear_fvg
                    lines.insert(
                        7,
                        f"🟥 Bearish FVG zone: ${f_lo:.2f}–${f_hi:.2f}",
                    )

                send_alert_text("\n".join(lines))
                _seen_short.add(sym)
                matched_syms.add(sym)
                alerts_sent += 1

        except Exception as exc:  # pragma: no cover - per-symbol resilience
            print(f"[opening_range_breakout] error on {sym}: {exc}")
            record_error(BOT_NAME, exc)
            continue

    finished = now_est_dt()
    runtime = time.time() - start_ts
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=scanned or len(universe),
            matched=len(matched_syms),
            alerts=alerts_sent,
            runtime_seconds=runtime,
            started_at=started_at,
            finished_at=finished,
        )
    except Exception as e:  # pragma: no cover - defensive
        print(f"[opening_range_breakout] record_bot_stats error: {e}")

    print("[opening_range_breakout] scan complete.")
