"""
Daily Ideas bot
---------------
Builds twice-daily confluence-based long/short ideas using:
    • Daily trend (20/50-day MAs)
    • Intraday move vs VWAP
    • Intraday RVOL
    • 5-minute RSI
    • Options flow bias (near-dated, near-the-money)

Runs in two EST windows (one pass per slot unless override enabled):
    • AM slot: 10:45–11:00
    • PM slot: 15:15–15:30

Outputs up to TOP_N_LONG / TOP_N_SHORT symbols with scores and context.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from polygon import RESTClient

from bots.options_common import iter_option_contracts
from bots.shared import (
    DEBUG_FLOW_REASONS,
    MIN_VOLUME_GLOBAL,
    chart_link,
    format_est_timestamp,
    minutes_since_midnight_est,
    now_est_dt,
    send_alert_text,
    record_bot_stats,
    resolve_universe_for_bot,
    today_est_date,
)
from bots.status_report import record_error

# ----------------- ENV / CONFIG -----------------

BOT_NAME = "daily_ideas"
STRATEGY_TAG = "DAILY_IDEAS"

POLYGON_KEY = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")
_CLIENT: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

DEFAULT_MAX_UNIVERSE = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
DAILY_IDEAS_MAX_UNIVERSE = int(os.getenv("DAILY_IDEAS_MAX_UNIVERSE", str(DEFAULT_MAX_UNIVERSE)))
DAILY_IDEAS_MIN_PRICE = float(os.getenv("DAILY_IDEAS_MIN_PRICE", "5.0"))
DAILY_IDEAS_MIN_DOLLAR_VOL = float(os.getenv("DAILY_IDEAS_MIN_DOLLAR_VOL", "200000"))
DAILY_IDEAS_MIN_SCORE = int(os.getenv("DAILY_IDEAS_MIN_SCORE", "3"))
DAILY_IDEAS_TOP_N = int(os.getenv("DAILY_IDEAS_TOP_N", "5"))
DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW = (
    os.getenv("DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW", "false").lower() == "true"
)

# Options bias thresholds
DAILY_IDEAS_OPT_MAX_DTE = int(os.getenv("DAILY_IDEAS_OPT_MAX_DTE", "30"))
DAILY_IDEAS_OPT_MONEINESS = float(os.getenv("DAILY_IDEAS_OPT_MONEINESS", "0.10"))
DAILY_IDEAS_OPT_MIN_NOTIONAL = float(os.getenv("DAILY_IDEAS_OPT_MIN_NOTIONAL", "50000"))

# Time slots (minutes since midnight ET)
AM_SLOT_START = 10 * 60 + 45
AM_SLOT_END = 11 * 60
PM_SLOT_START = 15 * 60 + 15
PM_SLOT_END = 15 * 60 + 30

# Scoring thresholds
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
LONG_MIN_THRESHOLD = DAILY_IDEAS_MIN_SCORE
SHORT_MIN_THRESHOLD = DAILY_IDEAS_MIN_SCORE

# Slot tracking (process memory; resets daily)
_last_run_day: Optional[date] = None
_ran_am: bool = False
_ran_pm: bool = False

# ----------------- HELPERS -----------------
eastern = pytz.timezone("US/Eastern")


def _reset_slots_if_new_day() -> None:
    global _last_run_day, _ran_am, _ran_pm
    today = today_est_date()
    if _last_run_day != today:
        _last_run_day = today
        _ran_am = False
        _ran_pm = False
        print("[daily_ideas] new trading day → reset AM/PM slot flags")


def _current_slot() -> Optional[str]:
    mins = minutes_since_midnight_est()
    if AM_SLOT_START <= mins <= AM_SLOT_END:
        return "am"
    if PM_SLOT_START <= mins <= PM_SLOT_END:
        return "pm"
    return None


def should_run_now() -> tuple[bool, Optional[str]]:
    # Always allow execution; gating happens inside run to ensure stats are recorded
    return True, None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Idea:
    sym: str
    score_bull: float
    score_bear: float
    pct_change: float
    rvol: float
    last: float
    open: float
    high: float
    low: float
    vwap: float
    rsi: Optional[float]
    trend_label: str
    trend_strength: float
    flow_bias: float

    def display_score(self, direction: str) -> float:
        return self.score_bull if direction == "long" else self.score_bear


# ----------------- DATA FETCH -----------------


def _fetch_daily_bars(sym: str, lookback: int = 80) -> List[Dict[str, Any]]:
    if not _CLIENT:
        return []
    end = today_est_date().isoformat()
    start = (today_est_date() - timedelta(days=lookback * 2)).isoformat()
    try:
        resp = list(
            _CLIENT.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                adjusted=True,
                sort="asc",
                limit=lookback * 2,
            )
        )
        bars: List[Dict[str, Any]] = []
        for a in resp:
            d = a.__dict__
            bars.append({"c": _safe_float(d.get("close") or d.get("c")), "v": _safe_float(d.get("volume") or d.get("v")) or 0.0})
        return [b for b in bars if b.get("c") is not None]
    except Exception as exc:
        print(f"[daily_ideas] daily bars error for {sym}: {exc}")
        return []


def _fetch_intraday_bars(sym: str, multiplier: int) -> List[Dict[str, Any]]:
    if not _CLIENT:
        return []
    start_dt = datetime.combine(today_est_date(), datetime.min.time()).replace(tzinfo=eastern)
    start_dt = start_dt.replace(hour=9, minute=30)
    end_dt = datetime.now(eastern)
    try:
        resp = list(
            _CLIENT.list_aggs(
                ticker=sym,
                multiplier=multiplier,
                timespan="minute",
                from_=int(start_dt.timestamp() * 1000),
                to=int(end_dt.timestamp() * 1000),
                adjusted=True,
                sort="asc",
                limit=5000,
            )
        )
        bars: List[Dict[str, Any]] = []
        for a in resp:
            d = a.__dict__
            bars.append(
                {
                    "o": _safe_float(d.get("open") or d.get("o")),
                    "h": _safe_float(d.get("high") or d.get("h")),
                    "l": _safe_float(d.get("low") or d.get("l")),
                    "c": _safe_float(d.get("close") or d.get("c")),
                    "v": _safe_float(d.get("volume") or d.get("v")) or 0.0,
                }
            )
        return [b for b in bars if b.get("c") is not None]
    except Exception as exc:
        print(f"[daily_ideas] intraday bars error for {sym}: {exc}")
        return []


# ----------------- SIGNAL COMPUTATION -----------------


def _compute_rsi(closes: List[float], period: int = RSI_PERIOD) -> List[float]:
    if len(closes) < period + 1:
        return []

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rsis: List[float] = [math.nan] * period
    rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    rsis.append(rsi)

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
        rsis.append(rsi)
    return rsis


def _intraday_rvol(day_vol: float, hist_vols: List[float]) -> float:
    vols = [v for v in hist_vols if v > 0]
    if not vols:
        return 1.0
    avg_vol = sum(vols) / len(vols)
    minutes_since_open = max(0, minutes_since_midnight_est() - (9 * 60 + 30))
    intraday_frac = min(1.0, minutes_since_open / 390.0)
    if intraday_frac <= 0:
        return 1.0
    expected = avg_vol * intraday_frac
    if expected <= 0:
        return 1.0
    return day_vol / expected


def _trend_from_daily(bars: List[Dict[str, Any]]) -> tuple[str, float]:
    if len(bars) < 50:
        return "sideways", 0.0
    closes = [b["c"] for b in bars if b.get("c") is not None]
    if len(closes) < 50:
        return "sideways", 0.0
    sma20 = sum(closes[-20:]) / 20.0
    sma50 = sum(closes[-50:]) / 50.0
    last = closes[-1]
    if last > sma20 > sma50:
        strength = (last - sma50) / sma50 * 100.0 if sma50 > 0 else 0.0
        return "up", strength
    if last < sma20 < sma50:
        strength = (sma50 - last) / sma50 * 100.0 if sma50 > 0 else 0.0
        return "down", -strength
    return "sideways", 0.0


def _options_flow_bias(sym: str, under_price: Optional[float]) -> float:
    if under_price is None or under_price <= 0:
        return 0.0
    contracts = iter_option_contracts(sym, ttl_seconds=60)
    call_notional = 0.0
    put_notional = 0.0
    for c in contracts:
        if c.premium is None or c.size is None:
            continue
        if c.dte is None or c.dte < 0 or c.dte > DAILY_IDEAS_OPT_MAX_DTE:
            continue
        if c.strike is None or c.strike <= 0:
            continue
        moneyness = abs(c.strike / under_price - 1.0)
        if moneyness > DAILY_IDEAS_OPT_MONEINESS:
            continue
        if c.notional is None or c.notional < DAILY_IDEAS_OPT_MIN_NOTIONAL:
            continue
        cp = (c.cp or "").upper()
        if cp in {"C", "CALL"}:
            call_notional += c.notional
        elif cp in {"P", "PUT"}:
            put_notional += c.notional
    denom = call_notional + put_notional
    if denom <= 0:
        return 0.0
    return (call_notional - put_notional) / denom


def _score_components(idea: Idea) -> tuple[float, float]:
    # Trend
    bull = 0.0
    bear = 0.0
    if idea.trend_label == "up":
        bull += 2
    elif idea.trend_label == "down":
        bear += 2

    # Intraday move + VWAP
    above_vwap = idea.last > idea.vwap
    if idea.pct_change >= 2 and above_vwap:
        bull += 2
    elif idea.pct_change > 0 and above_vwap:
        bull += 1
    if idea.pct_change <= -2 and not above_vwap:
        bear += 2
    elif idea.pct_change < 0 and not above_vwap:
        bear += 1

    # RVOL
    if idea.rvol >= 3:
        bull += 2
        bear += 2
    elif idea.rvol >= 2:
        bull += 1
        bear += 1

    # RSI
    if idea.rsi is not None:
        if 35 <= idea.rsi <= 60:
            bull += 2
        if idea.rsi >= 70:
            bear += 2

    # Options flow bias
    if idea.flow_bias >= 0.6:
        bull += 2
    elif idea.flow_bias >= 0.3:
        bull += 1
    elif idea.flow_bias <= -0.6:
        bear += 2
    elif idea.flow_bias <= -0.3:
        bear += 1

    return bull, bear


# ----------------- ALERT FORMATTING -----------------


def _fmt_price(value: Optional[float]) -> str:
    return "N/A" if value is None else f"${value:,.2f}"


def _format_idea_lines(direction: str, ideas: List[Idea]) -> List[str]:
    lines: List[str] = []
    for idx, idea in enumerate(ideas, start=1):
        score = idea.display_score(direction)
        lines.append(
            f"{idx}. {idea.sym} — Score: {score:.1f}"
        )
        lines.append(
            f"   Trend: {idea.trend_label} ({idea.trend_strength:+.1f}%)"
        )
        lines.append(
            f"   💵 Price: {_fmt_price(idea.last)} (O: {_fmt_price(idea.open)}, H: {_fmt_price(idea.high)}, L: {_fmt_price(idea.low)})"
        )
        lines.append(
            f"   📊 Intraday: {idea.pct_change:+.1f}% vs prior close, {'above' if idea.last > idea.vwap else 'below'} VWAP | RVOL {idea.rvol:.1f}x"
        )
        if idea.rsi is not None:
            lines.append(f"   🔍 RSI (5m): {idea.rsi:.1f}")
        lines.append(f"   🧩 Options flow bias: {idea.flow_bias:+.2f}")
        lines.append(f"   📈 Chart: {chart_link(idea.sym)}")
        lines.append("")
    return lines


def _build_alert(direction: str, ideas: List[Idea], timestamp: datetime) -> str:
    header = f"💡 DAILY IDEAS — {direction.upper()}S ({format_est_timestamp(timestamp)})"
    lines = [header, "────────────"]
    if ideas:
        lines.append(
            f"Top {len(ideas)} {direction.upper()} ideas (ranked by confluence score):"
        )
        lines.append("")
        lines.extend(_format_idea_lines(direction, ideas))
    else:
        lines.append(f"No high-confluence {direction.upper()} ideas for this slot.")
    return "\n".join(lines).strip()


# ----------------- MAIN BOT -----------------


async def run_daily_ideas() -> None:
    start_dt = now_est_dt()
    print(f"[daily_ideas] start {format_est_timestamp(start_dt)}")
    _reset_slots_if_new_day()

    slot = _current_slot()
    global _ran_am, _ran_pm

    scanned = 0
    matched = 0
    alerts_sent = 0

    if slot is None and not DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW:
        print("[daily_ideas] outside AM/PM window; skipping with zero stats")
        record_bot_stats(
            BOT_NAME,
            scanned=0,
            matched=0,
            alerts=0,
            started_at=start_dt,
            finished_at=now_est_dt(),
        )
        return

    if slot == "am" and _ran_am:
        print("[daily_ideas] AM slot already executed today; skipping")
        record_bot_stats(
            BOT_NAME,
            scanned=0,
            matched=0,
            alerts=0,
            started_at=start_dt,
            finished_at=now_est_dt(),
        )
        return
    if slot == "pm" and _ran_pm:
        print("[daily_ideas] PM slot already executed today; skipping")
        record_bot_stats(
            BOT_NAME,
            scanned=0,
            matched=0,
            alerts=0,
            started_at=start_dt,
            finished_at=now_est_dt(),
        )
        return

    if not POLYGON_KEY or not _CLIENT:
        print("[daily_ideas] POLYGON_KEY missing; cannot run")
        record_bot_stats(
            BOT_NAME,
            scanned=0,
            matched=0,
            alerts=0,
            started_at=start_dt,
            finished_at=now_est_dt(),
        )
        return

    universe = resolve_universe_for_bot(
        bot_name=BOT_NAME,
        bot_env_var="DAILY_IDEAS_TICKER_UNIVERSE",
        max_universe_env="DAILY_IDEAS_MAX_UNIVERSE",
        default_max_universe=DAILY_IDEAS_MAX_UNIVERSE,
        apply_dynamic_filters=True,
    )
    if not universe:
        print("[daily_ideas] empty universe; skipping")
        record_bot_stats(
            BOT_NAME,
            scanned=0,
            matched=0,
            alerts=0,
            started_at=start_dt,
            finished_at=now_est_dt(),
        )
        return

    long_ideas: List[Idea] = []
    short_ideas: List[Idea] = []

    for sym in universe:
        scanned += 1
        try:
            daily_bars = _fetch_daily_bars(sym)
            if not daily_bars:
                continue
            prev_close = daily_bars[-2]["c"] if len(daily_bars) >= 2 else daily_bars[-1]["c"]
            trend_label, trend_strength = _trend_from_daily(daily_bars)

            bars_1m = _fetch_intraday_bars(sym, multiplier=1)
            if not bars_1m:
                continue
            day_vol = sum(b["v"] for b in bars_1m)
            day_dollar_vol = sum((b["c"] or 0.0) * b["v"] for b in bars_1m)
            last_price = bars_1m[-1]["c"]
            first_open = bars_1m[0]["o"]
            day_high = max(b["h"] or 0 for b in bars_1m)
            day_low = min(b["l"] or 0 for b in bars_1m)

            if last_price is None or first_open is None:
                continue
            if last_price < DAILY_IDEAS_MIN_PRICE:
                continue
            if day_vol < MIN_VOLUME_GLOBAL or day_dollar_vol < DAILY_IDEAS_MIN_DOLLAR_VOL:
                continue

            vwap_num = sum((b["c"] or 0.0) * b["v"] for b in bars_1m)
            vwap_den = sum(b["v"] for b in bars_1m)
            vwap = vwap_num / vwap_den if vwap_den else last_price

            pct_change = 0.0
            if prev_close and prev_close > 0:
                pct_change = (last_price - prev_close) / prev_close * 100.0

            hist_vols = [b["v"] for b in daily_bars[-21:-1]]
            rvol = _intraday_rvol(day_vol, hist_vols)

            bars_5m = _fetch_intraday_bars(sym, multiplier=5)
            rsi_last: Optional[float] = None
            if bars_5m:
                closes_5m = [b["c"] for b in bars_5m if b.get("c") is not None]
                rsis = _compute_rsi(closes_5m, period=RSI_PERIOD)
                if rsis:
                    rsi_last = rsis[-1]

            flow_bias = _options_flow_bias(sym, last_price)

            idea = Idea(
                sym=sym,
                score_bull=0.0,
                score_bear=0.0,
                pct_change=pct_change,
                rvol=rvol,
                last=last_price,
                open=first_open,
                high=day_high,
                low=day_low,
                vwap=vwap,
                rsi=rsi_last,
                trend_label=trend_label,
                trend_strength=trend_strength,
                flow_bias=flow_bias,
            )

            bull_score, bear_score = _score_components(idea)
            idea.score_bull = bull_score
            idea.score_bear = bear_score
            if bull_score < LONG_MIN_THRESHOLD and bear_score < SHORT_MIN_THRESHOLD:
                continue

            matched += 1
            if bull_score > bear_score and bull_score >= LONG_MIN_THRESHOLD:
                long_ideas.append(idea)
            elif bear_score > bull_score and bear_score >= SHORT_MIN_THRESHOLD:
                short_ideas.append(idea)
        except Exception as exc:  # pragma: no cover - defensive
            if DEBUG_FLOW_REASONS:
                print(f"[daily_ideas] skip {sym}: {exc}")
            continue

    long_ideas.sort(key=lambda x: x.score_bull, reverse=True)
    short_ideas.sort(key=lambda x: x.score_bear, reverse=True)

    long_ideas = long_ideas[:DAILY_IDEAS_TOP_N]
    short_ideas = short_ideas[:DAILY_IDEAS_TOP_N]

    if long_ideas or short_ideas:
        if long_ideas:
            send_alert_text(_build_alert("long", long_ideas, start_dt))
            alerts_sent += 1
        else:
            send_alert_text(
                f"💡 DAILY IDEAS — LONGS ({format_est_timestamp(start_dt)})\n────────────\nNo high-confluence LONG ideas for this slot."
            )
            alerts_sent += 1

        if short_ideas:
            send_alert_text(_build_alert("short", short_ideas, start_dt))
            alerts_sent += 1
        else:
            send_alert_text(
                f"💡 DAILY IDEAS — SHORTS ({format_est_timestamp(start_dt)})\n────────────\nNo high-confluence SHORT ideas for this slot."
            )
            alerts_sent += 1
    else:
        send_alert_text(
            f"💡 DAILY IDEAS ({format_est_timestamp(start_dt)})\n────────────\nNo high-confluence ideas (long or short) for this slot."
        )
        alerts_sent += 1

    if slot == "am":
        _ran_am = True
    elif slot == "pm":
        _ran_pm = True

    finished = now_est_dt()
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=scanned,
            matched=matched,
            alerts=alerts_sent,
            started_at=start_dt,
            finished_at=finished,
        )
    except Exception as exc:
        print(f"[daily_ideas] record_bot_stats error: {exc}")
        try:
            record_error(BOT_NAME, exc)
        except Exception:
            pass

    print(
        f"[daily_ideas] done slot={slot or 'override'} scanned={scanned} matched={matched} alerts={alerts_sent}"
    )


if __name__ == "__main__":
    # Simple formatting demo
    demo_timestamp = now_est_dt()
    demo_idea = Idea(
        sym="AXSM",
        score_bull=9.1,
        score_bear=1.0,
        pct_change=6.5,
        rvol=6.3,
        last=182.64,
        open=158.49,
        high=184.40,
        low=158.49,
        vwap=170.12,
        rsi=54.2,
        trend_label="up",
        trend_strength=12.3,
        flow_bias=0.72,
    )
    print(_build_alert("long", [demo_idea], demo_timestamp))
