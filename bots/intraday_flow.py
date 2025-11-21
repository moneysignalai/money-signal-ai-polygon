# bots/intraday_flow.py
#
# Unified intraday price-action bot:
#   • ORB (Opening Range Breakout)
#   • Volume Monster
#   • Panic Flush
#   • Momentum Reversal
#
# Each strategy is time-gated and de-duped per day.

import os
from datetime import date, timedelta, datetime
from typing import List, Dict, Any, Optional, Tuple

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
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    grade_equity_setup,
    chart_link,
    now_est,
    minutes_since_midnight_est,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- GLOBAL / UNIVERSE ----------------

INTRADAY_MAX_UNIVERSE = int(os.getenv("INTRADAY_MAX_UNIVERSE", "120"))

def _get_universe() -> List[str]:
    env = os.getenv("INTRADAY_TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=INTRADAY_MAX_UNIVERSE, volume_coverage=0.95)

def _now_mins() -> int:
    return minutes_since_midnight_est()


def _time_str() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ---------------- INTRADAY FETCH ----------------

def _fetch_intraday(
    sym: str,
    trading_day: date,
    multiplier: int = 1,
    timespan: str = "minute",
    limit: int = 800,
) -> List[Any]:
    if not _client:
        return []
    try:
        aggs = _client.list_aggs(
            sym,
            multiplier,
            timespan,
            trading_day.isoformat(),
            trading_day.isoformat(),
            limit=limit,
            sort="asc",
        )
        bars = list(aggs)
    except Exception as e:
        print(f"[intraday_flow] intraday agg error for {sym}: {e}")
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
        mins = dt_et.hour * 60 + dt_et.minute
        if mins < 9 * 60 + 30 or mins > 16 * 60:
            continue
        b._et = dt_et
        filtered.append(b)
    return filtered


# ---------------- ORB CONFIG & LOGIC ----------------

ORB_START_MIN = 9 * 60 + 45   # 09:45
ORB_END_MIN   = 11 * 60       # 11:00

ORB_MIN_RVOL      = float(os.getenv("ORB_MIN_RVOL", "1.7"))
ORB_MIN_DOLLARVOL = float(os.getenv("ORB_MIN_DOLLARVOL", "5000000"))
ORB_MIN_BREAK_PCT = float(os.getenv("ORB_MIN_BREAK_PCT", "0.5"))  # % beyond range

_orb_date: date | None = None
_orb_alerted: set[str] = set()

def _reset_orb():
    global _orb_date, _orb_alerted
    today = date.today()
    if _orb_date != today:
        _orb_date = today
        _orb_alerted = set()

def _orb_in_window() -> bool:
    mins = _now_mins()
    return ORB_START_MIN <= mins <= ORB_END_MIN

def _run_orb_for_symbol(sym: str, trading_day: date, bars_5m: List[Any]):
    if not bars_5m:
        return
    if sym in _orb_alerted:
        return

    # First 3 x 5-min bars = 15-min OR range
    if len(bars_5m) < 3:
        return

    or_bars = [b for b in bars_5m if 9 * 60 + 30 <= (b._et.hour * 60 + b._et.minute) < 9 * 60 + 45]
    if len(or_bars) < 3:
        # just use earliest 3 bars if metadata weird
        or_bars = bars_5m[:3]

    or_high = max(float(getattr(b, "high", getattr(b, "h", 0))) for b in or_bars)
    or_low = min(float(getattr(b, "low", getattr(b, "l", 0))) for b in or_bars)

    last_bar = bars_5m[-1]
    last_price = float(getattr(last_bar, "close", getattr(last_bar, "c", 0)) or 0)
    if last_price <= 0:
        return

    # Day volume from 1-min bars to compute RVOL & dollar vol
    bars_1m = _fetch_intraday(sym, trading_day, multiplier=1, timespan="minute")
    if not bars_1m:
        return
    day_vol = float(sum(getattr(b, "volume", getattr(b, "v", 0)) for b in bars_1m))

    # RVOL vs last 20 days
    try:
        start = (trading_day - timedelta(days=40)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym, 1, "day", start, end, limit=50, sort="asc"
            )
        )
    except Exception as e:
        print(f"[intraday_flow:ORB] daily error for {sym}: {e}")
        return

    if not daily:
        return

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if recent:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0))) for d in recent) / len(recent)
    else:
        avg_vol = day_vol

    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
    if rvol < max(ORB_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    dollar_vol = last_price * day_vol
    if dollar_vol < max(ORB_MIN_DOLLARVOL, MIN_VOLUME_GLOBAL * last_price):
        return

    break_pct = 0.0
    direction = None
    if last_price > or_high:
        break_pct = (last_price - or_high) / or_high * 100.0
        direction = "UP"
    elif last_price < or_low:
        break_pct = (or_low - last_price) / or_low * 100.0
        direction = "DOWN"

    if not direction or break_pct < ORB_MIN_BREAK_PCT:
        return

    # Use last 5-min bar for intraday move from open
    day_open = float(getattr(bars_5m[0], "open", getattr(bars_5m[0], "o", 0)) or 0)
    if day_open <= 0:
        return
    intraday_pct = (last_price - day_open) / day_open * 100.0

    grade = grade_equity_setup(intraday_pct, rvol, dollar_vol)
    emoji = "🚀" if direction == "UP" else "🩸"
    label = "ORB Breakout" if direction == "UP" else "ORB Breakdown"

    body = (
        f"{emoji} {label}\n"
        f"📏 15-min Range: {or_low:.2f} – {or_high:.2f}\n"
        f"💰 Last: ${last_price:.2f} ({break_pct:.1f}% beyond range)\n"
        f"📊 Intraday vs open: {intraday_pct:.1f}%\n"
        f"📦 Day Volume: {int(day_vol):,}\n"
        f"📊 RVOL: {rvol:.1f}x · Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 ORB — {sym}\n"
        f"🕒 {_time_str()}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _orb_alerted.add(sym)
    send_alert("intraday_orb", sym, last_price, rvol, extra=extra)


# ---------------- VOLUME MONSTER CONFIG & LOGIC ----------------

MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "2000000"))
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "12000000"))
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))
MIN_VOLUME_RVOL = float(os.getenv("VOLUME_MIN_RVOL", "1.8"))

_vol_date: date | None = None
_vol_alerted: set[str] = set()

def _reset_vol():
    global _vol_date, _vol_alerted
    today = date.today()
    if _vol_date != today:
        _vol_date = today
        _vol_alerted = set()

def _find_monster_bar(sym: str, bars: List[Any], last_price: float) -> Tuple[bool, float]:
    if not bars or last_price <= 0:
        return False, 0.0
    vols = [float(getattr(b, "volume", getattr(b, "v", 0))) for b in bars]
    if not vols:
        return False, 0.0
    max_bar_vol = max(vols)
    dollar_bar = max_bar_vol * last_price
    if max_bar_vol < MIN_MONSTER_BAR_SHARES:
        return False, max_bar_vol
    if dollar_bar < MIN_MONSTER_DOLLAR_VOL:
        return False, max_bar_vol
    return True, max_bar_vol

def _compute_rvol_and_day_stats(sym: str, trading_day: date) -> Tuple[float, float, float, float, float]:
    bars = _fetch_intraday(sym, trading_day, multiplier=1, timespan="minute")
    if not bars:
        return 1.0, 0.0, 0.0, 0.0, 0.0

    day_vol = float(sum(getattr(b, "volume", getattr(b, "v", 0)) for b in bars))
    last_price = float(getattr(bars[-1], "close", getattr(bars[-1], "c", 0)) or 0)

    try:
        start = (trading_day - timedelta(days=30)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=50,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[intraday_flow:volume] daily agg error for {sym}: {e}")
        return 1.0, day_vol, last_price, 0.0, 0.0

    if not daily:
        return 1.0, day_vol, last_price, 0.0, 0.0

    d0 = daily[-1]
    prev_close = float(getattr(daily[-2], "close", getattr(daily[-2], "c", 0))) if len(daily) >= 2 else 0.0

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if not recent:
        avg_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)))
    else:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0))) for d in recent) / len(recent)

    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * day_vol
    return rvol, day_vol, last_price, prev_close, dollar_vol

def _run_volume_for_symbol(sym: str, trading_day: date):
    if sym in _vol_alerted:
        return

    rvol, day_vol, last_price, prev_close, dollar_vol = _compute_rvol_and_day_stats(sym, trading_day)
    if last_price <= 0 or prev_close <= 0:
        return
    if last_price < MIN_MONSTER_PRICE:
        return

    if rvol < MIN_VOLUME_RVOL:
        return
    if day_vol < MIN_VOLUME_GLOBAL:
        return
    if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
        return

    bars = _fetch_intraday(sym, trading_day, multiplier=1, timespan="minute")
    if not bars:
        return

    found, monster_bar_vol = _find_monster_bar(sym, bars, last_price)
    if not found:
        return

    move_pct = (last_price - prev_close) / prev_close * 100.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)
    bias = "Bullish accumulation" if move_pct >= 0 else "Bearish distribution"

    body = (
        f"💥 Monster Volume Spike Detected\n"
        f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
        f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
        f"📦 Biggest 1-min Bar: {int(monster_bar_vol):,} shares "
        f"(≈ ${monster_bar_vol * last_price:,.0f})\n"
        f"📊 RVOL: {rvol:.1f}x\n"
        f"🎯 Setup Grade: {grade}\n"
        f"📌 Bias: {bias}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 VOLUME — {sym}\n"
        f"🕒 {_time_str()}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _vol_alerted.add(sym)
    send_alert("intraday_volume", sym, last_price, rvol, extra=extra)


# ---------------- PANIC FLUSH CONFIG & LOGIC ----------------

PANIC_MIN_DROP_PCT   = float(os.getenv("PANIC_MIN_DROP_PCT", "12.0"))
PANIC_NEAR_LOW_PCT   = float(os.getenv("PANIC_NEAR_LOW_PCT", "10.0"))  # within 10% of 52w low
PANIC_MIN_RVOL       = float(os.getenv("PANIC_MIN_RVOL", "2.0"))
PANIC_MIN_DOLLAR_VOL = float(os.getenv("PANIC_MIN_DOLLAR_VOL", "15000000"))

_panic_date: date | None = None
_panic_alerted: set[str] = set()

def _reset_panic():
    global _panic_date, _panic_alerted
    today = date.today()
    if _panic_date != today:
        _panic_date = today
        _panic_alerted = set()

def _run_panic_flush_for_symbol(sym: str, trading_day: date):
    if sym in _panic_alerted:
        return

    try:
        start = (trading_day - timedelta(days=260)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym, 1, "day", start, end, limit=260, sort="asc"
            )
        )
    except Exception as e:
        print(f"[intraday_flow:panic] daily error for {sym}: {e}")
        return

    if len(daily) < 30:
        return

    d0 = daily[-1]
    prev = daily[-2]

    last_price = float(getattr(d0, "close", getattr(d0, "c", 0)) or 0)
    prev_close = float(getattr(prev, "close", getattr(prev, "c", 0)) or 0)
    day_low = float(getattr(d0, "low", getattr(d0, "l", 0)) or 0)
    day_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)) or 0)

    if last_price <= 0 or prev_close <= 0:
        return

    move_pct = (last_price - prev_close) / prev_close * 100.0
    if move_pct > -PANIC_MIN_DROP_PCT:
        return

    lows = [float(getattr(d, "low", getattr(d, "l", 0)) or 0) for d in daily[-252:]]
    lows = [x for x in lows if x > 0]
    if not lows:
        return

    low_52w = min(lows)
    if low_52w <= 0:
        return

    dist_to_low_pct = (last_price - low_52w) / low_52w * 100.0
    if dist_to_low_pct > PANIC_NEAR_LOW_PCT:
        return

    vols = [float(getattr(d, "volume", getattr(d, "v", 0)) or 0) for d in daily[-21:-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return

    rvol = day_vol / avg_vol
    if rvol < max(PANIC_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    dollar_vol = last_price * day_vol
    if dollar_vol < PANIC_MIN_DOLLAR_VOL:
        return

    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    body = (
        f"🩸 Panic Flush detected\n"
        f"📉 Move: {move_pct:.1f}% today\n"
        f"📉 52w Low: ${low_52w:.2f} (price is {dist_to_low_pct:.1f}% above)\n"
        f"📦 Volume: {int(day_vol):,} (RVOL {rvol:.1f}x, ≈ ${dollar_vol:,.0f})\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 PANIC FLUSH — {sym}\n"
        f"🕒 {_time_str()}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _panic_alerted.add(sym)
    send_alert("intraday_panic_flush", sym, last_price, rvol, extra=extra)


# ---------------- MOMENTUM REVERSAL CONFIG & LOGIC ----------------

MREV_MIN_TREND_PCT    = float(os.getenv("MREV_MIN_TREND_PCT", "6.0"))   # move from prev close before reversal
MREV_MIN_REVERSAL_PCT = float(os.getenv("MREV_MIN_REVERSAL_PCT", "3.0")) # intraday off high/low
MREV_MIN_RVOL         = float(os.getenv("MREV_MIN_RVOL", "1.8"))
MREV_MIN_DOLLAR_VOL   = float(os.getenv("MREV_MIN_DOLLAR_VOL", "10000000"))

_mrev_date: date | None = None
_mrev_alerted: set[str] = set()

def _reset_mrev():
    global _mrev_date, _mrev_alerted
    today = date.today()
    if _mrev_date != today:
        _mrev_date = today
        _mrev_alerted = set()

def _run_momentum_reversal_for_symbol(sym: str, trading_day: date, bars_5m: List[Any]):
    if sym in _mrev_alerted:
        return
    if not bars_5m:
        return

    # Daily history for prev close & RVOL
    try:
        start = (trading_day - timedelta(days=40)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                sym, 1, "day", start, end, limit=50, sort="asc"
            )
        )
    except Exception as e:
        print(f"[intraday_flow:mrev] daily error for {sym}: {e}")
        return

    if len(daily) < 2:
        return

    d0 = daily[-1]
    prev = daily[-2]

    last_price = float(getattr(d0, "close", getattr(d0, "c", 0)) or 0)
    prev_close = float(getattr(prev, "close", getattr(prev, "c", 0)) or 0)
    day_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)) or 0)

    if last_price <= 0 or prev_close <= 0:
        return

    day_move_pct = (last_price - prev_close) / prev_close * 100.0

    vols = [float(getattr(d, "volume", getattr(d, "v", 0)) or 0) for d in daily[-21:-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return
    rvol = day_vol / avg_vol
    if rvol < max(MREV_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    dollar_vol = last_price * day_vol
    if dollar_vol < MREV_MIN_DOLLAR_VOL:
        return

    # From intraday bars, get high and low of the day so far
    highs = [float(getattr(b, "high", getattr(b, "h", 0)) or 0) for b in bars_5m]
    lows = [float(getattr(b, "low", getattr(b, "l", 0)) or 0) for b in bars_5m]
    if not highs or not lows:
        return

    intraday_high = max(highs)
    intraday_low = min(lows)

    if intraday_high <= 0 or intraday_low <= 0:
        return

    # Reversal from high (for strong uptrend then fade)
    from_high_pct = (intraday_high - last_price) / intraday_high * 100.0
    # Reversal from low (for strong dump then squeeze)
    from_low_pct = (last_price - intraday_low) / intraday_low * 100.0

    direction = None
    reversal_pct = 0.0
    label = ""

    # First, require it was strongly trending intraday from prev close
    if abs(day_move_pct) < MREV_MIN_TREND_PCT:
        return

    if day_move_pct > 0 and from_high_pct >= MREV_MIN_REVERSAL_PCT:
        direction = "DOWN"
        reversal_pct = from_high_pct
        label = "Momentum Fade from High"
    elif day_move_pct < 0 and from_low_pct >= MREV_MIN_REVERSAL_PCT:
        direction = "UP"
        reversal_pct = from_low_pct
        label = "Momentum Reversal Bounce from Low"
    else:
        return

    grade = grade_equity_setup(day_move_pct, rvol, dollar_vol)
    emoji = "⚠️"

    body = (
        f"{emoji} {label}\n"
        f"📉 Day move from prev close: {day_move_pct:.1f}%\n"
        f"🔁 Reversal off extreme: {reversal_pct:.1f}%\n"
        f"📏 Intraday Range: Low {intraday_low:.2f} – High {intraday_high:.2f}\n"
        f"📦 Volume: {int(day_vol):,} (RVOL {rvol:.1f}x, ≈ ${dollar_vol:,.0f})\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 MOMENTUM REVERSAL — {sym}\n"
        f"🕒 {_time_str()}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _mrev_alerted.add(sym)
    send_alert("intraday_momentum_reversal", sym, last_price, rvol, extra=extra)


# ---------------- MAIN ENTRYPOINT ----------------

async def run_intraday_flow():
    """
    Unified intraday engine:

      • Time gates:
          - ORB: 09:45–11:00
          - Volume Monster: 09:30–16:00
          - Panic Flush: 09:30–16:00
          - Momentum Reversal: 11:30–16:00
      • Universe: INTRADAY_TICKER_UNIVERSE env OR dynamic top volume universe.
    """
    if not POLYGON_KEY or not _client:
        print("[intraday_flow] Missing client/API key.")
        return

    mins = _now_mins()
    trading_day = date.today()

    # Time windows
    in_rth = 9 * 60 + 30 <= mins <= 16 * 60
    in_orb = ORB_START_MIN <= mins <= ORB_END_MIN
    in_mrev = 11 * 60 + 30 <= mins <= 16 * 60

    if not in_rth:
        print("[intraday_flow] outside RTH; skipping.")
        return

    _reset_orb()
    _reset_vol()
    _reset_panic()
    _reset_mrev()

    universe = _get_universe()
    if not universe:
        print("[intraday_flow] empty universe; skipping.")
        return

    print(f"[intraday_flow] scanning {len(universe)} symbols at {_time_str()}")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # shared bars: 5-min for ORB & momentum reversal
        bars_5m: List[Any] = []
        if in_orb or in_mrev:
            bars_5m = _fetch_intraday(sym, trading_day, multiplier=5, timespan="minute")

        # ORB (early window only)
        if in_orb:
            _run_orb_for_symbol(sym, trading_day, bars_5m)

        # Volume Monster
        _run_volume_for_symbol(sym, trading_day)

        # Panic Flush
        _run_panic_flush_for_symbol(sym, trading_day)

        # Momentum Reversal
        if in_mrev:
            _run_momentum_reversal_for_symbol(sym, trading_day, bars_5m)

    print("[intraday_flow] scan complete.")
