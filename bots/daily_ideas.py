# bots/daily_ideas.py
#
# Daily Ideas bot with simple "confluence" scoring:
#   • Trend (daily)
#   • Intraday move + VWAP
#   • Intraday RVOL
#   • RSI (5-min)
#   • Options flow bias (near-dated, near-the-money calls vs puts)
#
# Runs only twice per day (US/Eastern):
#   • AM slot: ~10:45–11:00
#   • PM slot: ~15:15–15:30
#
# Produces:
#   • One alert with top LONG ideas
#   • One alert with top SHORT ideas

import os
import time
import math
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz

from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
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

# -------- ENV CONFIG --------

DAILY_IDEAS_MIN_PRICE = float(os.getenv("DAILY_IDEAS_MIN_PRICE", "5.0"))
DAILY_IDEAS_MIN_DOLLAR_VOL = float(os.getenv("DAILY_IDEAS_MIN_DOLLAR_VOL", "200000"))
DAILY_IDEAS_MAX_UNIVERSE = int(os.getenv("DAILY_IDEAS_MAX_UNIVERSE", "80"))

DAILY_IDEAS_MIN_SCORE = int(os.getenv("DAILY_IDEAS_MIN_SCORE", "3"))
DAILY_IDEAS_TOP_N = int(os.getenv("DAILY_IDEAS_TOP_N", "5"))

# Options flow bias thresholds
DAILY_IDEAS_OPT_MAX_DTE = int(os.getenv("DAILY_IDEAS_OPT_MAX_DTE", "30"))
DAILY_IDEAS_OPT_MONEINESS = float(os.getenv("DAILY_IDEAS_OPT_MONEINESS", "0.10"))  # 10% from ATM
DAILY_IDEAS_OPT_MIN_NOTIONAL = float(os.getenv("DAILY_IDEAS_OPT_MIN_NOTIONAL", "50000"))

# Time slots (minutes since midnight ET)
AM_SLOT_START = 10 * 60 + 45  # 10:45
AM_SLOT_END   = 11 * 60 + 0   # 11:00
PM_SLOT_START = 15 * 60 + 15  # 15:15
PM_SLOT_END   = 15 * 60 + 30  # 15:30

DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW = (
    os.getenv("DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW", "false").lower() == "true"
)

# Track which slots have already run for the day
_last_run_day: Optional[date] = None
_ran_am: bool = False
_ran_pm: bool = False


def _reset_slots_if_new_day() -> None:
    global _last_run_day, _ran_am, _ran_pm
    today = today_est_date()
    if _last_run_day != today:
        _last_run_day = today
        _ran_am = False
        _ran_pm = False
        print("[daily_ideas] New trading day – reset run slots.")


def _current_slot() -> Optional[str]:
    mins = minutes_since_midnight_est()
    if AM_SLOT_START <= mins <= AM_SLOT_END:
        return "am"
    if PM_SLOT_START <= mins <= PM_SLOT_END:
        return "pm"
    return None


def should_run_now() -> tuple[bool, str | None]:
    if DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW:
        return True, None
    slot = _current_slot()
    if slot:
        return True, None
    return False, "outside daily idea window"


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _fetch_intraday_bars_1m(sym: str, trading_day: date) -> List[Dict[str, Any]]:
    """
    Fetch 1-minute bars from 09:30 to now.
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
            if ts > 1e12:  # ms
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
        return [b for b in bars if b["c"] is not None]
    except Exception as e:
        print(f"[daily_ideas] error fetching 1-min for {sym}: {e}")
        return []


def _fetch_intraday_bars_5m(sym: str, trading_day: date) -> List[Dict[str, Any]]:
    """
    Fetch 5-minute bars from 09:30 to now.
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
            multiplier=5,
            timespan="minute",
            from_=start_ms,
            to=end_ms,
            adjusted=True,
            sort="asc",
            limit=1000,
        )
        bars: List[Dict[str, Any]] = []
        for a in resp:
            d = a.__dict__
            ts = d.get("timestamp") or d.get("t")
            if ts is None:
                continue
            if ts > 1e12:  # ms
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
        return [b for b in bars if b["c"] is not None]
    except Exception as e:
        print(f"[daily_ideas] error fetching 5-min for {sym}: {e}")
        return []


def _compute_rvol(sym: str, trading_day: date, day_vol: float) -> float:
    """
    Simple 20-day RVOL estimate using daily bars.
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

        hist = daily[1:] if len(daily) > 1 else daily
        vols = []
        for b in hist:
            v = _safe_float(getattr(b, "volume", getattr(b, "v", None)))
            if v and v > 0:
                vols.append(v)
        if not vols:
            return 1.0

        avg_vol = sum(vols) / len(vols)

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
        print(f"[daily_ideas] RVOL error for {sym}: {e}")
        return 1.0


def _compute_trend(sym: str, trading_day: date) -> Tuple[str, float]:
    """
    Simple trend using 20-day and 50-day SMAs.

    Returns (trend_label, trend_score):
        trend_label in {"up", "down", "sideways"}
        trend_score positive for bullish, negative for bearish magnitude.
    """
    if not _client:
        return "sideways", 0.0

    try:
        start = (trading_day - timedelta(days=80)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                adjusted=True,
                sort="asc",
                limit=80,
            )
        )
        if len(daily) < 50:
            return "sideways", 0.0

        closes = [
            _safe_float(getattr(b, "close", getattr(b, "c", None))) for b in daily
        ]
        closes = [c for c in closes if c is not None]
        if len(closes) < 50:
            return "sideways", 0.0

        last = closes[-1]
        sma20 = sum(closes[-20:]) / 20.0
        sma50 = sum(closes[-50:]) / 50.0

        if last > sma20 > sma50:
            # uptrend
            strength = (last - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
            return "up", strength
        if last < sma20 < sma50:
            # downtrend
            strength = (sma50 - last) / sma50 * 100.0 if sma50 > 0 else 0.0
            return "down", -strength

        return "sideways", 0.0
    except Exception as e:
        print(f"[daily_ideas] trend error for {sym}: {e}")
        return "sideways", 0.0


def _compute_rsi(closes: List[float], period: int = 14) -> List[float]:
    """
    Standard RSI implementation. Returns list of same length as closes.
    """
    if len(closes) < period + 1:
        return []

    rsis: List[float] = []
    gains: List[float] = []
    losses: List[float] = []

    # first period
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

    # subsequent
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


def _options_bias(sym: str, trading_day: date, under_price: float) -> Tuple[float, float]:
    """
    Approximate options flow bias from snapshot chain.

    Returns (call_notional, put_notional) for:
      • near-dated (<= DAILY_IDEAS_OPT_MAX_DTE),
      • near-the-money (<= DAILY_IDEAS_OPT_MONEINESS),
      • with at least DAILY_IDEAS_OPT_MIN_NOTIONAL per contract.
    """
    chain = get_option_chain_cached(sym)
    if not chain:
        return 0.0, 0.0

    today = trading_day
    opts = chain.get("results") or chain.get("result") or chain.get("options") or []
    if not isinstance(opts, list):
        return 0.0, 0.0

    call_notional = 0.0
    put_notional = 0.0

    for opt in opts:
        details = opt.get("details") or {}
        ua = opt.get("underlying_asset") or {}
        day = opt.get("day") or {}

        try:
            strike = float(details.get("strike_price") or details.get("strike") or 0.0)
        except Exception:
            strike = 0.0
        if under_price <= 0 or strike <= 0:
            continue

        moneyness = abs(strike / under_price - 1.0)
        if moneyness > DAILY_IDEAS_OPT_MONEINESS:
            continue

        exp_str = details.get("expiration_date")
        if not exp_str:
            continue
        try:
            y, m, d = [int(x) for x in exp_str.split("-")]
            expiry = date(y, m, d)
        except Exception:
            continue

        dte = (expiry - today).days
        if dte < 0 or dte > DAILY_IDEAS_OPT_MAX_DTE:
            continue

        ct = (details.get("contract_type") or "").upper()
        if ct not in ("CALL", "PUT"):
            continue

        vol = _safe_float(day.get("volume")) or 0.0
        last = _safe_float(day.get("close") or day.get("open")) or 0.0
        if vol <= 0 or last <= 0:
            continue

        notional = vol * last * 100.0
        if notional < DAILY_IDEAS_OPT_MIN_NOTIONAL:
            continue

        if ct == "CALL":
            call_notional += notional
        else:
            put_notional += notional

    return call_notional, put_notional


async def run_daily_ideas() -> None:
    """
    Build confluence-based daily idea lists (LONG and SHORT).
    """
    _reset_slots_if_new_day()

    slot = _current_slot()
    global _ran_am, _ran_pm

    if slot is None and not DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW:
        print("[daily_ideas] outside idea windows; skipping.")
        record_bot_stats("Daily Ideas", 0, 0, 0, 0.0)
        return
    if slot is None and DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW:
        slot = "override"
    if slot == "am" and _ran_am:
        print("[daily_ideas] AM slot already ran; skipping.")
        record_bot_stats("Daily Ideas", 0, 0, 0, 0.0)
        return
    if slot == "pm" and _ran_pm:
        print("[daily_ideas] PM slot already ran; skipping.")
        record_bot_stats("Daily Ideas", 0, 0, 0, 0.0)
        return

    if not POLYGON_KEY or not _client:
        print("[daily_ideas] missing POLYGON_KEY or client; skipping.")
        record_bot_stats("Daily Ideas", 0, 0, 0, 0.0)
        return

    BOT_NAME = "daily_ideas"
    start_ts = time.time()
    alerts_sent = 0
    scanned = 0
    matched = 0

    trading_day = today_est_date()
    now_str = now_est()

    universe = get_dynamic_top_volume_universe(
        max_tickers=DAILY_IDEAS_MAX_UNIVERSE,
        volume_coverage=0.90,
    )
    if not universe:
        print("[daily_ideas] empty universe; skipping.")
        record_bot_stats("Daily Ideas", 0, 0, 0, 0.0)
        return

    print(f"[daily_ideas] start slot={slot} universe_size={len(universe)}")

    long_ideas: List[Dict[str, Any]] = []
    short_ideas: List[Dict[str, Any]] = []

    for sym in universe:
        scanned += 1
        if is_etf_blacklisted(sym):
            continue

        bars_1m = _fetch_intraday_bars_1m(sym, trading_day)
        if not bars_1m:
            continue

        day_vol = sum(b["v"] for b in bars_1m)
        day_dollar_vol = sum((b["c"] or 0.0) * b["v"] for b in bars_1m)
        last_price = bars_1m[-1]["c"]
        first_open = bars_1m[0]["o"]

        if last_price is None or first_open is None:
            continue
        if last_price < DAILY_IDEAS_MIN_PRICE:
            continue
        if day_vol < MIN_VOLUME_GLOBAL or day_dollar_vol < DAILY_IDEAS_MIN_DOLLAR_VOL:
            continue

        rvol = _compute_rvol(sym, trading_day, day_vol)
        if rvol < MIN_RVOL_GLOBAL:
            continue

        # basic intraday stats
        pct_change = (last_price - first_open) / first_open * 100.0
        vwap_num = sum((b["c"] or 0.0) * b["v"] for b in bars_1m)
        vwap_den = sum(b["v"] for b in bars_1m) or 1.0
        vwap = vwap_num / vwap_den

        # trend (daily)
        trend_label, trend_strength = _compute_trend(sym, trading_day)

        # RSI on 5-min bars
        bars_5m = _fetch_intraday_bars_5m(sym, trading_day)
        closes_5m = [b["c"] for b in bars_5m if b["c"] is not None]
        if len(closes_5m) < 16:
            continue
        rsis = _compute_rsi(closes_5m, 14)
        if not rsis or len(rsis) != len(closes_5m):
            continue
        rsi_last = rsis[-1]
        rsi_prev = rsis[-2]
        if math.isnan(rsi_last) or math.isnan(rsi_prev):
            continue

        # options bias (near-dated, near-ATM)
        call_notional, put_notional = _options_bias(sym, trading_day, last_price)

        # ---------- SCORING ----------
        bull_score = 0
        bear_score = 0

        # Intraday move
        if pct_change >= 2.0:
            bull_score += 1
        elif pct_change <= -2.0:
            bear_score += 1

        # VWAP bias
        if last_price > vwap:
            bull_score += 1
        elif last_price < vwap:
            bear_score += 1

        # Trend
        if trend_label == "up":
            bull_score += 1
        elif trend_label == "down":
            bear_score += 1

        # RSI timing
        if rsi_last <= 40 and rsi_last > rsi_prev:
            bull_score += 1
        if rsi_last >= 60 and rsi_last < rsi_prev:
            bear_score += 1

        # Options flow bias
        if call_notional >= 2 * put_notional and call_notional >= DAILY_IDEAS_OPT_MIN_NOTIONAL:
            bull_score += 2
        elif put_notional >= 2 * call_notional and put_notional >= DAILY_IDEAS_OPT_MIN_NOTIONAL:
            bear_score += 2
        elif call_notional > put_notional and call_notional >= DAILY_IDEAS_OPT_MIN_NOTIONAL:
            bull_score += 1
        elif put_notional > call_notional and put_notional >= DAILY_IDEAS_OPT_MIN_NOTIONAL:
            bear_score += 1

        max_score = max(bull_score, bear_score)
        if max_score < DAILY_IDEAS_MIN_SCORE or bull_score == bear_score:
            continue

        idea = {
            "sym": sym,
            "score_bull": bull_score,
            "score_bear": bear_score,
            "pct_change": pct_change,
            "rvol": rvol,
            "last_price": last_price,
            "vwap": vwap,
            "trend_label": trend_label,
            "trend_strength": trend_strength,
            "rsi_last": rsi_last,
            "call_notional": call_notional,
            "put_notional": put_notional,
        }

        matched += 1
        if bull_score > bear_score:
            long_ideas.append(idea)
        else:
            short_ideas.append(idea)

    # Sort & select top N
    long_ideas.sort(key=lambda x: x["score_bull"], reverse=True)
    short_ideas.sort(key=lambda x: x["score_bear"], reverse=True)

    long_ideas = long_ideas[:DAILY_IDEAS_TOP_N]
    short_ideas = short_ideas[:DAILY_IDEAS_TOP_N]

    # ---------- ALERTS ----------

    # LONGS
    if long_ideas:
        lines = [
            "📈 Daily LONG Ideas (confluence)",
            f"🕒 {now_str}",
            "────────────",
        ]
        for idx, idea in enumerate(long_ideas, start=1):
            sym = idea["sym"]
            bs = idea["score_bull"]
            pct = idea["pct_change"]
            rsi = idea["rsi_last"]
            trend = idea["trend_label"]
            cs = idea["call_notional"]
            ps = idea["put_notional"]
            above_vwap = "above" if idea["last_price"] > idea["vwap"] else "below"
            lines.append(
                f"{idx}. {sym} — score {bs} | {pct:.1f}% on day | RSI {rsi:.1f} | "
                f"Trend: {trend} | Price {above_vwap} VWAP | "
                f"Call flow ≈ ${cs:,.0f}, Put flow ≈ ${ps:,.0f} | "
                f"Chart: {chart_link(sym)}"
            )
        extra = "\n".join(lines)
        first = long_ideas[0]
        send_alert(
            "daily_ideas_long",
            first["sym"],
            first["last_price"],
            first["rvol"],
            extra=extra,
        )
        alerts_sent += 1

    # SHORTS
    if short_ideas:
        lines = [
            "📉 Daily SHORT / Hedge Ideas (confluence)",
            f"🕒 {now_str}",
            "────────────",
        ]
        for idx, idea in enumerate(short_ideas, start=1):
            sym = idea["sym"]
            bs = idea["score_bear"]
            pct = idea["pct_change"]
            rsi = idea["rsi_last"]
            trend = idea["trend_label"]
            cs = idea["call_notional"]
            ps = idea["put_notional"]
            below_vwap = "below" if idea["last_price"] < idea["vwap"] else "above"
            lines.append(
                f"{idx}. {sym} — score {bs} | {pct:.1f}% on day | RSI {rsi:.1f} | "
                f"Trend: {trend} | Price {below_vwap} VWAP | "
                f"Call flow ≈ ${cs:,.0f}, Put flow ≈ ${ps:,.0f} | "
                f"Chart: {chart_link(sym)}"
            )
        extra = "\n".join(lines)
        first = short_ideas[0]
        send_alert(
            "daily_ideas_short",
            first["sym"],
            first["last_price"],
            first["rvol"],
            extra=extra,
        )
        alerts_sent += 1

    # Mark slot as used
    if slot == "am":
        _ran_am = True
    elif slot == "pm":
        _ran_pm = True

    runtime = time.time() - start_ts
    try:
        record_bot_stats(
            "daily_ideas",
            scanned=scanned,
            matched=matched,
            alerts=alerts_sent,
            runtime=runtime,
        )
    except Exception as e:
        print(f"[daily_ideas] record_bot_stats error: {e}")

    print("[daily_ideas] scan complete.")