# bots/intraday_flow.py
#
# Unified intraday stock flow bot:
#   • Volume Monster (1-min “monster bar” + big RVOL)
#   • Panic Flush (capitulation down days near lows with huge RVOL)
#   • Momentum Reversal (big move that starts reversing intraday)
#
# All share the same Polygon data pull per symbol to keep it efficient.

import os
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

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
)

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- GLOBAL CONFIG ----------------

INTRADAY_START_MIN = 9 * 60 + 30   # 09:30
INTRADAY_END_MIN   = 16 * 60       # 16:00

# Universe
INTRADAY_MAX_UNIVERSE = int(os.getenv("INTRADAY_MAX_UNIVERSE", "120"))

# -------- Volume Monster config --------
VM_MIN_MONSTER_BAR_SHARES = float(os.getenv("VM_MIN_MONSTER_BAR_SHARES", "2000000"))
VM_MIN_MONSTER_DOLLAR_VOL = float(os.getenv("VM_MIN_MONSTER_DOLLAR_VOL", "12000000"))
VM_MIN_MONSTER_PRICE      = float(os.getenv("VM_MIN_MONSTER_PRICE", "2.0"))
VM_MIN_VOLUME_RVOL        = float(os.getenv("VM_MIN_VOLUME_RVOL", "1.8"))

# -------- Panic Flush config --------
PF_MIN_PRICE          = float(os.getenv("PF_MIN_PRICE", "3.0"))
PF_MIN_DROP_PCT       = float(os.getenv("PF_MIN_DROP_PCT", "12.0"))   # -12% or worse
PF_MIN_RVOL           = float(os.getenv("PF_MIN_RVOL", "3.0"))
PF_MIN_DOLLAR_VOL     = float(os.getenv("PF_MIN_DOLLAR_VOL", "15000000"))
PF_LOOKBACK_DAYS      = int(os.getenv("PF_LOOKBACK_DAYS", "250"))     # ~1 year
PF_NEAR_LOW_PCT       = float(os.getenv("PF_NEAR_LOW_PCT", "10.0"))   # within 10% of 52w low

# -------- Momentum Reversal config --------
MR_MIN_PRICE          = float(os.getenv("MR_MIN_PRICE", "3.0"))
MR_MIN_RVOL           = float(os.getenv("MR_MIN_RVOL", "2.0"))
MR_MIN_DOLLAR_VOL     = float(os.getenv("MR_MIN_DOLLAR_VOL", "10000000"))
MR_TREND_PCT          = float(os.getenv("MR_TREND_PCT", "8.0"))       # initial trend magnitude
MR_RETRACE_PCT        = float(os.getenv("MR_RETRACE_PCT", "5.0"))     # reversal off high/low
MR_SCAN_START_MIN     = 11 * 60 + 30  # after 11:30 only

# ---------------- STATE (per strategy) ----------------

_alert_date: Optional[date] = None
_seen_vm: set[str] = set()
_seen_pf: set[str] = set()
_seen_mr: set[str] = set()


def _reset_day() -> None:
    global _alert_date, _seen_vm, _seen_pf, _seen_mr
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _seen_vm = set()
        _seen_pf = set()
        _seen_mr = set()


def _in_intraday_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return INTRADAY_START_MIN <= mins <= INTRADAY_END_MIN


def _in_mr_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return MR_SCAN_START_MIN <= mins <= INTRADAY_END_MIN


# ---------------- HELPERS ----------------

def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _get_universe() -> List[str]:
    env = os.getenv("INTRADAY_FLOW_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=INTRADAY_MAX_UNIVERSE, volume_coverage=0.95)


def _fetch_intraday(sym: str, trading_day: date) -> List[Any]:
    """
    1-min bars for current day (RTH only filter done later).
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
        print(f"[intraday_flow] intraday agg error for {sym}: {e}")
        return []

    filtered: List[Any] = []
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
        # only RTH
        mins = dt_et.hour * 60 + dt_et.minute
        if mins < INTRADAY_START_MIN or mins > INTRADAY_END_MIN:
            continue
        b._et = dt_et
        filtered.append(b)
    return filtered


def _fetch_daily_history(sym: str, trading_day: date, days_back: int) -> List[Any]:
    if not _client:
        return []
    try:
        start = (trading_day - timedelta(days=days_back + 5)).isoformat()
        end = trading_day.isoformat()
        daily = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                limit=days_back + 10,
                sort="asc",
            )
        )
        return daily
    except Exception as e:
        print(f"[intraday_flow] daily agg error for {sym}: {e}")
        return []


def _compute_core_stats(sym: str, trading_day: date) -> Optional[Dict[str, Any]]:
    """
    Shared daily stats:
      - prev_close, open, last, high, low
      - vol_today, rvol_20d, dollar_vol
      - 52w_low (for panic flush)
    """
    daily = _fetch_daily_history(sym, trading_day, max(PF_LOOKBACK_DAYS, 40))
    if len(daily) < 5:
        return None

    today_bar = daily[-1]
    # ensure today's bar is current trading day
    ts = getattr(today_bar, "timestamp", getattr(today_bar, "t", None))
    if ts is not None:
        if ts > 1e12:
            ts = ts / 1000.0
        dt_utc = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
        if dt_utc.astimezone(eastern).date() != trading_day:
            return None

    prev_bar = daily[-2]

    last_price = _safe_float(getattr(today_bar, "close", getattr(today_bar, "c", None)))
    open_today = _safe_float(getattr(today_bar, "open", getattr(today_bar, "o", None)))
    day_high = _safe_float(getattr(today_bar, "high", getattr(today_bar, "h", None)))
    day_low = _safe_float(getattr(today_bar, "low", getattr(today_bar, "l", None)))
    vol_today = _safe_float(getattr(today_bar, "volume", getattr(today_bar, "v", None)))
    prev_close = _safe_float(getattr(prev_bar, "close", getattr(prev_bar, "c", None)))

    if (
        last_price is None or open_today is None or prev_close is None
        or vol_today is None or day_high is None or day_low is None
    ):
        return None

    if prev_close <= 0 or last_price <= 0:
        return None

    # 20-day RVOL
    hist = daily[:-1]
    recent = hist[-20:] if len(hist) > 20 else hist
    if recent:
        avg_vol = sum(
            float(getattr(d, "volume", getattr(d, "v", 0.0)))
            for d in recent
        ) / float(len(recent))
    else:
        avg_vol = vol_today

    rvol = vol_today / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * vol_today

    # 52w low approx (from PF_LOOKBACK_DAYS)
    long_hist = daily[-PF_LOOKBACK_DAYS:] if len(daily) > PF_LOOKBACK_DAYS else daily
    low_52w = min(
        float(getattr(d, "low", getattr(d, "l", getattr(d, "c", 0.0))))
        for d in long_hist
    )

    return {
        "prev_close": prev_close,
        "open_today": open_today,
        "last_price": last_price,
        "day_high": day_high,
        "day_low": day_low,
        "vol_today": vol_today,
        "rvol": rvol,
        "dollar_vol": dollar_vol,
        "low_52w": low_52w,
    }


def _format_time() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ---------------- STRATEGIES ----------------

def _check_volume_monster(sym: str, stats: Dict[str, Any], bars: List[Any]) -> Optional[str]:
    """
    Volume Monster:
      - Price >= VM_MIN_MONSTER_PRICE
      - RVOL >= VM_MIN_VOLUME_RVOL (and MIN_RVOL_GLOBAL)
      - Day volume & dollar volume big
      - At least one 1-min bar with huge shares and dollar volume
    """
    if sym in _seen_vm:
        return None

    last_price = stats["last_price"]
    rvol = stats["rvol"]
    day_vol = stats["vol_today"]
    dollar_vol = stats["dollar_vol"]
    prev_close = stats["prev_close"]

    if last_price < VM_MIN_MONSTER_PRICE:
        return None

    if rvol < max(VM_MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL):
        return None

    if day_vol < MIN_VOLUME_GLOBAL:
        return None

    if dollar_vol < VM_MIN_MONSTER_DOLLAR_VOL:
        return None

    if not bars:
        return None

    vols = [
        float(getattr(b, "volume", getattr(b, "v", 0.0)))
        for b in bars
    ]
    if not vols:
        return None

    max_bar_vol = max(vols)
    bar_dollar = max_bar_vol * last_price

    if max_bar_vol < VM_MIN_MONSTER_BAR_SHARES:
        return None
    if bar_dollar < VM_MIN_MONSTER_DOLLAR_VOL:
        return None

    move_pct = (last_price - prev_close) / prev_close * 100.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    body = (
        f"💥 VOLUME MONSTER — {sym}\n"
        f"🕒 {_format_time()}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
        f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
        f"📦 Biggest 1-min Bar: {int(max_bar_vol):,} shares "
        f"(≈ ${bar_dollar:,.0f})\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    _seen_vm.add(sym)
    send_alert("intraday_volume_monster", sym, last_price, rvol, extra=body)
    return "volume_monster"


def _check_panic_flush(sym: str, stats: Dict[str, Any]) -> Optional[str]:
    """
    Panic Flush:
      - Price >= PF_MIN_PRICE
      - Huge down move vs prior close (<= -PF_MIN_DROP_PCT)
      - Near 52w low
      - Very high RVOL + dollar volume
    """
    if sym in _seen_pf:
        return None

    last_price = stats["last_price"]
    prev_close = stats["prev_close"]
    day_low = stats["day_low"]
    vol_today = stats["vol_today"]
    rvol = stats["rvol"]
    dollar_vol = stats["dollar_vol"]
    low_52w = stats["low_52w"]

    if last_price < PF_MIN_PRICE:
        return None

    move_pct = (last_price - prev_close) / prev_close * 100.0
    if move_pct > -PF_MIN_DROP_PCT:
        return None

    if rvol < max(PF_MIN_RVOL, MIN_RVOL_GLOBAL):
        return None

    if vol_today < MIN_VOLUME_GLOBAL:
        return None

    if dollar_vol < PF_MIN_DOLLAR_VOL:
        return None

    # Near 52w low
    if low_52w <= 0:
        return None
    distance_from_low_pct = (last_price - low_52w) / low_52w * 100.0
    if distance_from_low_pct > PF_NEAR_LOW_PCT:
        return None

    wick_pct = (last_price - day_low) / last_price * 100.0 if last_price > 0 else 0.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    body = (
        f"🩸 PANIC FLUSH — {sym}\n"
        f"🕒 {_format_time()}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"📉 Move vs prior close: {move_pct:.1f}%\n"
        f"📉 52w Low: ${low_52w:.2f} (distance {distance_from_low_pct:.1f}% above)\n"
        f"📏 Day Range: Low ${day_low:.2f} → Last ${last_price:.2f} (lower wick ~{wick_pct:.1f}%)\n"
        f"📦 Day Volume: {int(vol_today):,} (≈ ${dollar_vol:,.0f})\n"
        f"🎯 Setup Grade: {grade} · Bias: capitulation / potential bounce zone\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    _seen_pf.add(sym)
    send_alert("intraday_panic_flush", sym, last_price, rvol, extra=body)
    return "panic_flush"


def _check_momentum_reversal(sym: str, stats: Dict[str, Any]) -> Optional[str]:
    """
    Momentum Reversal:
      Two-sided logic:
        • Blow-off top: big up move vs prior close, last price retracing hard from day high.
        • Flush reversal: big down move vs prior close, last price bouncing hard off day low.
      Only scanned after MR_SCAN_START_MIN (late morning / afternoon).
    """
    if sym in _seen_mr:
        return None
    if not _in_mr_window():
        return None

    last_price = stats["last_price"]
    prev_close = stats["prev_close"]
    day_high = stats["day_high"]
    day_low = stats["day_low"]
    vol_today = stats["vol_today"]
    rvol = stats["rvol"]
    dollar_vol = stats["dollar_vol"]

    if last_price < MR_MIN_PRICE:
        return None
    if rvol < max(MR_MIN_RVOL, MIN_RVOL_GLOBAL):
        return None
    if vol_today < MIN_VOLUME_GLOBAL:
        return None
    if dollar_vol < MR_MIN_DOLLAR_VOL:
        return None

    move_pct = (last_price - prev_close) / prev_close * 100.0
    # Blow-off top reversal
    top_reversal = False
    bottom_reversal = False

    if day_high > 0:
        run_up_pct = (day_high - prev_close) / prev_close * 100.0
        retrace_from_high_pct = (day_high - last_price) / day_high * 100.0
        if run_up_pct >= MR_TREND_PCT and retrace_from_high_pct >= MR_RETRACE_PCT and move_pct > 0:
            top_reversal = True

    if day_low > 0:
        selloff_pct = (day_low - prev_close) / prev_close * 100.0  # negative
        bounce_from_low_pct = (last_price - day_low) / day_low * 100.0
        if selloff_pct <= -MR_TREND_PCT and bounce_from_low_pct >= MR_RETRACE_PCT and move_pct < 0:
            bottom_reversal = True

    if not top_reversal and not bottom_reversal:
        return None

    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    if top_reversal:
        direction_text = "UPTREND → Bearish Reversal (blow-off top fading)"
        emoji = "⚠️"
    else:
        direction_text = "DOWNTREND → Bullish Reversal (flush bounce)"
        emoji = "🔄"

    body = (
        f"{emoji} MOMENTUM REVERSAL — {sym}\n"
        f"🕒 {_format_time()}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"📌 {direction_text}\n"
        f"📊 Move vs prior close: {move_pct:.1f}%\n"
        f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f}\n"
        f"📦 Day Volume: {int(vol_today):,} (≈ ${dollar_vol:,.0f})\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    _seen_mr.add(sym)
    send_alert("intraday_momentum_reversal", sym, last_price, rvol, extra=body)
    return "momentum_reversal"

#------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
from bots.status_report import record_bot_stats

BOT_NAME = "intraday_flow"
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


# ---------------- MAIN ENTRY ----------------

async def run_intraday_flow() -> None:
    """
    Unified intraday stock flow bot.

    For each symbol in the universe:
      • Compute daily stats (once).
      • Fetch 1-min bars (once).
      • Run:
          - Volume Monster
          - Panic Flush
          - Momentum Reversal
    Dedupes per symbol per day per strategy.
    """
    _reset_day()

    if not POLYGON_KEY or not _client:
        print("[intraday_flow] missing POLYGON_KEY or REST client; skipping.")
        return

    if not _in_intraday_window():
        print("[intraday_flow] outside intraday window; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[intraday_flow] empty universe; skipping.")
        return

    trading_day = date.today()
    print(f"[intraday_flow] scanning {len(universe)} symbols")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        stats = _compute_core_stats(sym, trading_day)
        if not stats:
            continue

        # Volume Monster & Panic Flush only need daily stats
        bars = _fetch_intraday(sym, trading_day)

        _check_volume_monster(sym, stats, bars)
        _check_panic_flush(sym, stats)
        _check_momentum_reversal(sym, stats)

    print("[intraday_flow] scan complete.")
