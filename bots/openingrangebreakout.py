# bots/OpeningRangeBreak.py
#
# Opening Range Breakout (ORB) bot — STOCKS ONLY
#
# - Defines the first 15 minutes (09:30–09:45 ET) as the opening range.
# - Looks for clean breaks above the ORB high or below the ORB low.
# - Adds RVOL + dollar volume filters so you don’t get junk.
# - Single alert per symbol per day.

import os
import time
from datetime import datetime, date, timedelta
from typing import Any, List, Optional

import pytz

try:
    from massive import RESTClient
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
    is_etf_blacklisted,
)

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------

# ORB window (minutes after 09:30 open)
ORB_MINUTES = int(os.getenv("ORB_MINUTES", "15"))  # 15-min ORB

# ORB scan time window (est)
ORB_SCAN_START_MIN = 9 * 60 + 35   # start scanning slightly after 09:35
ORB_SCAN_END_MIN   = 11 * 60       # stop by 11:00

# Break buffer (to avoid tiny fake breaks)
ORB_BREAK_BUFFER_PCT = float(os.getenv("ORB_BREAK_BUFFER_PCT", "0.2"))  # 0.2% above/below

# Price / Volume filters
ORB_MIN_PRICE       = float(os.getenv("ORB_MIN_PRICE", "3.0"))
ORB_MAX_PRICE       = float(os.getenv("ORB_MAX_PRICE", "500.0"))
ORB_MIN_RVOL        = float(os.getenv("ORB_MIN_RVOL", "1.8"))
ORB_MIN_DOLLAR_VOL  = float(os.getenv("ORB_MIN_DOLLAR_VOL", "5000000"))  # $5M+

# Universe size
ORB_MAX_UNIVERSE    = int(os.getenv("ORB_MAX_UNIVERSE", "120"))

# Per-day de-dupe (symbol)
_alert_date: Optional[date] = None
_alerted_syms: set[str] = set()


# ---------------- STATE / TIME ----------------

def _reset_day() -> None:
    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    return sym in _alerted_syms


def _mark(sym: str) -> None:
    _alerted_syms.add(sym)


def _in_orb_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return ORB_SCAN_START_MIN <= mins <= ORB_SCAN_END_MIN


# ---------------- HELPERS ----------------

def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _get_universe() -> List[str]:
    env = os.getenv("ORB_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=ORB_MAX_UNIVERSE, volume_coverage=0.90)


def _fetch_intraday(sym: str, trading_day: date) -> List[Any]:
    """
    Fetch 1-min intraday bars for the current trading day.
    """
    if not _client:
        return []

    try:
        aggs = _client.list_aggs(
            ticker=sym,
            multiplier=1,
            timespan="minute",
            from_=trading_day.isoformat(),
            to=trading_day.isoformat(),
            limit=800,
            sort="asc",
        )
        bars = list(aggs)
    except Exception as e:
        print(f"[OpeningRangeBreak] intraday agg error for {sym}: {e}")
        return []

    filtered = []
    for b in bars:
        ts = getattr(b, "timestamp", getattr(b, "t", None))
        if ts is None:
            continue
        if ts > 1e12:  # ms → s
            ts = ts / 1000.0
        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        dt_et = dt_utc.astimezone(eastern)
        if dt_et.date() != trading_day:
            continue
        b._et = dt_et
        filtered.append(b)
    return filtered


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
                limit=50,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[OpeningRangeBreak] daily agg error for {sym}: {e}")
        return 1.0

    if not daily:
        return 1.0

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if not recent:
        avg_vol = day_vol
    else:
        avg_vol = sum(
            float(getattr(d, "volume", getattr(d, "v", 0.0)))
            for d in recent
        ) / float(len(recent))

    if avg_vol <= 0:
        return 1.0

    return day_vol / avg_vol


def _format_time() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ---------------- MAIN BOT ----------------

async def run_opening_range_break() -> None:
    """
    Opening Range Breakout bot.

    - Defines 15-min opening range (configurable).
    - Looks for clean break above ORB high or below ORB low.
    - Applies RVOL + dollar volume + price filters.
    """
    _reset_day()

    if not POLYGON_KEY or not _client:
        print("[OpeningRangeBreak] missing POLYGON_KEY or client; skipping.")
        return

    if not _in_orb_window():
        print("[OpeningRangeBreak] outside ORB scan window; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[OpeningRangeBreak] empty universe; skipping.")
        return

    trading_day = date.today()
    time_str = _format_time()

    print(f"[OpeningRangeBreak] scanning {len(universe)} symbols")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        bars = _fetch_intraday(sym, trading_day)
        if not bars:
            continue

        #------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
from bots.status_report import record_bot_stats

BOT_NAME = "opening_range_breakout"
...
start_ts = time.time()
alerts_sent = 0
matches = []

# ... your scan logic ...

run_seconds = time.time() - start_ts

record_bot_stats(
    BOT_NAME,
    scanned=len(universe),
    matched=len(matches),
    alerts=alerts_sent,
    runtime=run_seconds,
)

        # Split bars into ORB window and all RTH bars for today
        orb_start = datetime(trading_day.year, trading_day.month, trading_day.day, 9, 30, tzinfo=eastern)
        orb_end = orb_start + timedelta(minutes=ORB_MINUTES)

        orb_high = None
        orb_low = None
        last_price = None
        day_vol = 0.0

        for b in bars:
            dt_et = getattr(b, "_et", None)
            if dt_et is None:
                continue

            price = _safe_float(getattr(b, "close", getattr(b, "c", None)))
            vol = _safe_float(getattr(b, "volume", getattr(b, "v", None))) or 0.0
            if price is None:
                continue

            day_vol += vol
            last_price = price

            if orb_start <= dt_et < orb_end:
                if orb_high is None or price > orb_high:
                    orb_high = price
                if orb_low is None or price < orb_low:
                    orb_low = price

        if (
            orb_high is None
            or orb_low is None
            or last_price is None
            or day_vol <= 0
        ):
            continue

        if last_price < ORB_MIN_PRICE or last_price > ORB_MAX_PRICE:
            continue

        # Dollar volume + RVOL
        dollar_vol = last_price * day_vol
        if dollar_vol < max(ORB_MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
            continue

        rvol = _compute_rvol(sym, trading_day, day_vol)
        if rvol < max(ORB_MIN_RVOL, MIN_RVOL_GLOBAL):
            continue

        # Determine breakout direction
        buffer_up = orb_high * (1.0 + ORB_BREAK_BUFFER_PCT / 100.0)
        buffer_down = orb_low * (1.0 - ORB_BREAK_BUFFER_PCT / 100.0)

        direction = None
        emoji = None

        if last_price > buffer_up:
            direction = "Opening Range BREAKOUT"
            emoji = "🚀"
        elif last_price < buffer_down:
            direction = "Opening Range BREAKDOWN"
            emoji = "🩸"

        if not direction:
            continue

        range_pct = (orb_high - orb_low) / orb_low * 100.0 if orb_low > 0 else 0.0

        body_lines = [
            f"{emoji} ORB — {sym}",
            f"🕒 {time_str}",
            f"💰 Price: ${last_price:.2f} · RVOL {rvol:.1f}x",
            "────────────",
            f"📌 {direction}",
            f"📏 ORB Range: {orb_low:.2f} → {orb_high:.2f} (~{range_pct:.1f}%)",
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f})",
            f"🔗 Chart: {chart_link(sym)}",
        ]

        extra_text = "\n".join(body_lines)

        send_alert("opening_range_break", sym, last_price, rvol, extra=extra_text)
        _mark(sym)

    print("[OpeningRangeBreak] scan complete.")
