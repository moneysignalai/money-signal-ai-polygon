# bots/squeeze.py — Stock SHORT-SQUEEZE style bot (price + volume only)

import os
import time
from datetime import datetime, date, timedelta
from typing import List, Dict, Any, Optional

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    chart_link,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG ----------------
#
# These are tunable via env if you want more / less aggression.

# RTH window (squeeze bot is intraday, focuses on regular hours only)
RTH_START_MIN = 9 * 60 + 30      # 09:30
RTH_END_MIN   = 16 * 60          # 16:00

# Price filters
SQUEEZE_MIN_PRICE        = float(os.getenv("SQUEEZE_MIN_PRICE", "3.0"))
SQUEEZE_MAX_PRICE        = float(os.getenv("SQUEEZE_MAX_PRICE", "200.0"))

# Move filters (vs prior close + from today’s open)
SQUEEZE_MIN_MOVE_PCT     = float(os.getenv("SQUEEZE_MIN_MOVE_PCT", "15.0"))  # vs yesterday close
SQUEEZE_MIN_INTRADAY_PCT = float(os.getenv("SQUEEZE_MIN_INTRADAY_PCT", "8.0"))   # from today open

# Volume / RVOL filters
SQUEEZE_MIN_RVOL         = float(os.getenv("SQUEEZE_MIN_RVOL", "3.0"))      # day RVOL vs 20d avg
SQUEEZE_MIN_DOLLAR_VOL   = float(os.getenv("SQUEEZE_MIN_DOLLAR_VOL", "20000000"))  # $20M+
SQUEEZE_LOOKBACK_DAYS    = int(os.getenv("SQUEEZE_LOOKBACK_DAYS", "40"))    # daily history window

# Breakout context: require price near recent range highs
SQUEEZE_RECENT_WINDOW    = int(os.getenv("SQUEEZE_RECENT_WINDOW", "20"))    # days for recent high
SQUEEZE_NEAR_HIGH_PCT    = float(os.getenv("SQUEEZE_NEAR_HIGH_PCT", "10.0"))  # within 10% of recent high

# Universe size
SQUEEZE_MAX_UNIVERSE     = int(os.getenv("SQUEEZE_MAX_UNIVERSE", "80"))

# Per-day de-dupe (symbol)
_alert_date: Optional[date] = None
_alerted_syms: set[str] = set()


# ---------------- STATE ----------------

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


def _in_rth_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return RTH_START_MIN <= mins <= RTH_END_MIN


# ---------------- HELPERS ----------------

def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _fetch_daily_history(sym: str, trading_day: date) -> List[Any]:
    """Fetch recent daily bars for the symbol."""
    if not _client:
        return []

    try:
        start = (trading_day - timedelta(days=SQUEEZE_LOOKBACK_DAYS + 5)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                limit=SQUEEZE_LOOKBACK_DAYS + 10,
                sort="asc",
            )
        )
        return daily
    except Exception as e:
        print(f"[squeeze] daily agg error for {sym}: {e}")
        return []


def _compute_rvol_and_stats(sym: str, trading_day: date):
    """
    Compute price/volume stats and RVOL for today.

    Returns dict or None:
      {
        "prev_close", "open_today", "last_price",
        "day_high", "day_low",
        "vol_today", "rvol", "dollar_vol",
        "move_pct", "intraday_pct",
        "recent_high"
      }
    """
    daily = _fetch_daily_history(sym, trading_day)
    if len(daily) < 5:
        return None

    # Today = last bar that matches trading_day
    today_bar = daily[-1]
    # try to ensure it's actually today's bar (Polygon daily sometimes lags when closed)
    ts = getattr(today_bar, "timestamp", getattr(today_bar, "t", None))
    if ts is not None:
        if ts > 1e12:  # ms → s
            ts = ts / 1000.0
        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        if dt_utc.astimezone(eastern).date() != trading_day:
            # No current-day bar yet (pre-market / weekend)
            return None

    prev_bar = daily[-2]

    last_price = _safe_float(getattr(today_bar, "close", getattr(today_bar, "c", None)))
    open_today = _safe_float(getattr(today_bar, "open", getattr(today_bar, "o", None)))
    day_high = _safe_float(getattr(today_bar, "high", getattr(today_bar, "h", None)))
    day_low = _safe_float(getattr(today_bar, "low", getattr(today_bar, "l", None)))
    vol_today = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))
    prev_close = _safe_float(getattr(prev_bar, "close", getattr(prev_bar, "c", None)))

    if (
        last_price is None
        or open_today is None
        or prev_close is None
        or vol_today is None
        or day_high is None
        or day_low is None
    ):
        return None

    if prev_close <= 0 or open_today <= 0 or last_price <= 0:
        return None

    # Move % vs yesterday close and vs today's open
    move_pct = (last_price - prev_close) / prev_close * 100.0
    intraday_pct = (last_price - open_today) / open_today * 100.0

    # RVOL vs last ~20 days (excluding today)
    hist = daily[:-1]
    recent_hist = hist[-20:] if len(hist) > 20 else hist
    if recent_hist:
        avg_vol = sum(
            float(getattr(d, "volume", getattr(d, "v", 0.0)))
            for d in recent_hist
        ) / float(len(recent_hist))
    else:
        avg_vol = vol_today

    if avg_vol <= 0:
        rvol = 1.0
    else:
        rvol = vol_today / avg_vol

    dollar_vol = last_price * vol_today

    # Recent high (for breakout / squeeze context)
    recent_window = hist[-SQUEEZE_RECENT_WINDOW:] if len(hist) > SQUEEZE_RECENT_WINDOW else hist
    if recent_window:
        recent_high = max(
            float(getattr(d, "close", getattr(d, "c", 0.0)))
            for d in recent_window
        )
    else:
        recent_high = last_price

    return {
        "prev_close": prev_close,
        "open_today": open_today,
        "last_price": last_price,
        "day_high": day_high,
        "day_low": day_low,
        "vol_today": vol_today,
        "rvol": rvol,
        "dollar_vol": dollar_vol,
        "move_pct": move_pct,
        "intraday_pct": intraday_pct,
        "recent_high": recent_high,
    }


def _format_time() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")
        
        #------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
from bots.status_report import record_bot_stats

BOT_NAME = "squeeze"
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


# ---------------- MAIN BOT ----------------

async def run_squeeze() -> None:
    """
    Short SQUEEZE-style stock bot (no options).

    Looks for:
      • Big % move vs yesterday close (default ≥ 15%)
      • Strong move from today's open (default ≥ 8%)
      • Huge RVOL (default ≥ 3x 20-day avg)
      • Solid dollar volume (default ≥ $20M)
      • Price near recent range highs (within 10% of last ~20-day high)
      • 1 alert per symbol per day.
    """
    _reset_day()

    if not POLYGON_KEY or not _client:
        print("[squeeze] missing POLYGON_KEY or REST client; skipping.")
        return

    if not _in_rth_window():
        print("[squeeze] outside RTH; skipping.")
        return

    # Resolve universe
    env = os.getenv("SQUEEZE_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        universe = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        universe = get_dynamic_top_volume_universe(
            max_tickers=SQUEEZE_MAX_UNIVERSE,
            volume_coverage=0.90,
        )

    if not universe:
        print("[squeeze] empty universe; skipping.")
        return

    print(f"[squeeze] scanning {len(universe)} symbols for squeeze-style moves")

    trading_day = date.today()
    time_str = _format_time()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        stats = _compute_rvol_and_stats(sym, trading_day)
        if not stats:
            continue

        last_price = stats["last_price"]
        prev_close = stats["prev_close"]
        open_today = stats["open_today"]
        day_high = stats["day_high"]
        day_low = stats["day_low"]
        vol_today = stats["vol_today"]
        rvol = stats["rvol"]
        dollar_vol = stats["dollar_vol"]
        move_pct = stats["move_pct"]
        intraday_pct = stats["intraday_pct"]
        recent_high = stats["recent_high"]

        # Basic price filters
        if last_price < SQUEEZE_MIN_PRICE or last_price > SQUEEZE_MAX_PRICE:
            continue

        # Big % pop vs yesterday
        if move_pct < SQUEEZE_MIN_MOVE_PCT:
            continue

        # Strong continuation from the open (gap-and-go type behavior)
        if intraday_pct < SQUEEZE_MIN_INTRADAY_PCT:
            continue

        # RVOL + Dollar volume gates
        if rvol < max(SQUEEZE_MIN_RVOL, MIN_RVOL_GLOBAL):
            continue

        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        if dollar_vol < SQUEEZE_MIN_DOLLAR_VOL:
            continue

        # Breakout context: price near recent highs (not just dead-cat bounces)
        # Require last_price within SQUEEZE_NEAR_HIGH_PCT of recent_high
        if recent_high > 0:
            distance_from_high_pct = (recent_high - last_price) / recent_high * 100.0
        else:
            distance_from_high_pct = 0.0

        if distance_from_high_pct > SQUEEZE_NEAR_HIGH_PCT:
            # Too far below recent highs → more likely generic bounce than squeeze-style breakout
            continue

        # If we reach here: strong squeeze-style profile
        # Build alert body
        emoji = "🧨"
        rocket = "🚀"
        divider = "────────────"

        body_lines = [
            f"{emoji} SHORT SQUEEZE PROFILE — {sym}",
            f"🕒 {time_str}",
            f"💰 Price: ${last_price:.2f}",
            divider,
            f"{rocket} Move vs prior close: {move_pct:.1f}%",
            f"📈 From open: {intraday_pct:.1f}%  (Open ${open_today:.2f} → Last ${last_price:.2f})",
            f"📊 RVOL: {rvol:.1f}x · Day Vol: {int(vol_today):,}",
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}",
            f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f}",
            f"🏁 Recent High (~{SQUEEZE_RECENT_WINDOW}d): ${recent_high:.2f} "
            f"(distance {distance_from_high_pct:.1f}% below)",
            "📌 Bias: potential short/gamma squeeze style breakout",
            f"🔗 Chart: {chart_link(sym)}",
        ]

        extra_text = "\n".join(body_lines)

        # rvol passed into send_alert so it shows in the header if needed
        send_alert("squeeze", sym, last_price, rvol, extra=extra_text)
        _mark(sym)

    print("[squeeze] scan complete.")
