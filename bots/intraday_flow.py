# bots/intraday_flow.py
#
# Unified Intraday Strategy:
#   • ORB (Opening Range Breakout / Breakdown)
#   • Panic Flush (intraday rug pulls)
#   • Momentum Reversal (intraday oversold/overbought reversals)
#
# One pass over the universe, reusing intraday minute bars + daily stats.

import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple

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

# ======================================================
# CONFIG — all tunable via ENV
# ======================================================

# General intraday filters
INTRADAY_MIN_PRICE = float(os.getenv("INTRADAY_MIN_PRICE", "3.0"))
INTRADAY_MIN_DOLLAR_VOL = float(os.getenv("INTRADAY_MIN_DOLLAR_VOL", "5000000"))  # $5M+
INTRADAY_MAX_UNIVERSE = int(os.getenv("INTRADAY_MAX_UNIVERSE", "150"))

# Time window (RTH)
INTRADAY_SCAN_START_MIN = int(os.getenv("INTRADAY_SCAN_START_MIN", str(9 * 60 + 30)))  # 09:30
INTRADAY_SCAN_END_MIN = int(os.getenv("INTRADAY_SCAN_END_MIN", str(16 * 60)))          # 16:00

# ORB config
ORB_WINDOW_MIN = int(os.getenv("ORB_WINDOW_MIN", "15"))  # first 15 minutes
ORB_MIN_RVOL = float(os.getenv("ORB_MIN_RVOL", "1.5"))
ORB_MIN_DOLLAR_VOL = float(os.getenv("ORB_MIN_DOLLAR_VOL", "8000000"))  # $8M
ORB_BREAK_MIN_PCT = float(os.getenv("ORB_BREAK_MIN_PCT", "0.3"))        # 0.3% beyond range
ORB_MIN_SESSION_MOVE_PCT = float(os.getenv("ORB_MIN_SESSION_MOVE_PCT", "1.5"))

# Panic Flush (intraday)
FLUSH_MIN_RVOL = float(os.getenv("FLUSH_MIN_RVOL", "2.0"))
FLUSH_DROP_MIN_PCT = float(os.getenv("FLUSH_DROP_MIN_PCT", "5.0"))      # vs previous close
FLUSH_MAX_RSI = float(os.getenv("FLUSH_MAX_RSI", "30.0"))               # oversold
FLUSH_BAR_DROP_MIN_PCT = float(os.getenv("FLUSH_BAR_DROP_MIN_PCT", "1.5"))
FLUSH_BAR_VOL_MULT = float(os.getenv("FLUSH_BAR_VOL_MULT", "2.0"))      # vs avg 20 bars

# Momentum Reversal (intraday)
REV_MIN_RVOL = float(os.getenv("REV_MIN_RVOL", "1.5"))
REV_OVERSOLD_RSI = float(os.getenv("REV_OVERSOLD_RSI", "35.0"))
REV_OVERBOUGHT_RSI = float(os.getenv("REV_OVERBOUGHT_RSI", "70.0"))
REV_MIN_BOUNCE_PCT = float(os.getenv("REV_MIN_BOUNCE_PCT", "1.0"))      # from intraday low
REV_MIN_FADE_PCT = float(os.getenv("REV_MIN_FADE_PCT", "1.0"))          # from intraday high

# Universe
INTRADAY_TICKER_UNIVERSE = os.getenv("INTRADAY_TICKER_UNIVERSE")

# De-dupe per day / per strategy
_alert_date: Optional[date] = None
_alerted_orb: set[str] = set()
_alerted_flush: set[str] = set()
_alerted_rev: set[str] = set()


# ======================================================
# DAY RESET + TIME
# ======================================================

def _reset_if_new_day():
    global _alert_date, _alerted_orb, _alerted_flush, _alerted_rev
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_orb = set()
        _alerted_flush = set()
        _alerted_rev = set()


def _in_intraday_window() -> bool:
    mins = minutes_since_midnight_est()
    return INTRADAY_SCAN_START_MIN <= mins <= INTRADAY_SCAN_END_MIN


def _format_now_est() -> str:
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ======================================================
# UNIVERSE / DATA
# ======================================================

def _get_universe() -> List[str]:
    # 1) Intraday-specific override
    if INTRADAY_TICKER_UNIVERSE:
        return [s.strip().upper() for s in INTRADAY_TICKER_UNIVERSE.split(",") if s.strip()]

    # 2) Global env
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]

    # 3) Dynamic top-volume
    return get_dynamic_top_volume_universe(
        max_tickers=INTRADAY_MAX_UNIVERSE,
        volume_coverage=0.95,
    )


def _fetch_intraday(sym: str, trading_day: date):
    """Fetch 1-min intraday bars for the given trading day, RTH only."""
    if not _client:
        return []

    try:
        aggs = _client.list_aggs(
            sym,
            1,
            "minute",
            trading_day.isoformat(),
            trading_day.isoformat(),
            limit=800,
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


def _fetch_daily_stats(sym: str, trading_day: date) -> Optional[Tuple[float, float, float]]:
    """
    Return (prev_close, rvol, dollar_vol_today) based on daily bars.
    """
    if not _client:
        return None

    try:
        start = (trading_day - timedelta(days=40)).isoformat()
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
        print(f"[intraday_flow] daily agg error for {sym}: {e}")
        return None

    if len(daily) < 2:
        return None

    today_bar = daily[-1]
    prev_bar = daily[-2]

    prev_close = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0)) or 0.0)
    vol_today = float(getattr(today_bar, "volume", getattr(today_bar, "v", 0)) or 0.0)
    last_price = float(getattr(today_bar, "close", getattr(today_bar, "c", 0)) or 0.0)

    hist = daily[:-1]
    recent = hist[-20:] if len(hist) > 20 else hist
    if recent:
        avg_vol = sum(float(getattr(d, "volume", getattr(d, "v", 0)) or 0.0) for d in recent) / len(recent)
    else:
        avg_vol = vol_today

    rvol = vol_today / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * vol_today

    return prev_close, rvol, dollar_vol


def _compute_rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ======================================================
# STRATEGIES
# ======================================================

def _maybe_orb(sym: str, closes: List[float], bars: List, prev_close: float, rvol: float, dollar_vol: float):
    if sym in _alerted_orb:
        return
    if not bars or len(bars) < 10:
        return

    last_bar = bars[-1]
    last_price = float(getattr(last_bar, "close", getattr(last_bar, "c", 0)) or 0.0)
    if last_price < INTRADAY_MIN_PRICE:
        return
    if rvol < max(ORB_MIN_RVOL, MIN_RVOL_GLOBAL):
        return
    if dollar_vol < max(ORB_MIN_DOLLAR_VOL, INTRADAY_MIN_DOLLAR_VOL):
        return

    # Build opening range (first ORB_WINDOW_MIN minutes from 09:30)
    orb_start = 9 * 60 + 30
    orb_end = orb_start + ORB_WINDOW_MIN

    orb_bars = [b for b in bars if orb_start <= (b._et.hour * 60 + b._et.minute) < orb_end]
    if len(orb_bars) < 3:
        return

    orb_high = max(float(getattr(b, "high", getattr(b, "h", 0)) or 0.0) for b in orb_bars)
    orb_low = min(float(getattr(b, "low", getattr(b, "l", 0)) or 0.0) for b in orb_bars)

    if orb_high <= 0 or orb_low <= 0:
        return

    # Check if latest close broke above/below ORB by at least ORB_BREAK_MIN_PCT
    above_break = last_price > orb_high * (1.0 + ORB_BREAK_MIN_PCT / 100.0)
    below_break = last_price < orb_low * (1.0 - ORB_BREAK_MIN_PCT / 100.0)

    if not above_break and not below_break:
        return

    move_pct = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
    if abs(move_pct) < ORB_MIN_SESSION_MOVE_PCT:
        return

    direction = "ORB Breakout" if above_break else "ORB Breakdown"
    emoji = "🚀" if above_break else "🩸"

    grade = grade_equity_setup(move_pct, rvol, dollar_vol)
    ts = _format_now_est()

    body = (
        f"{emoji} {direction}\n"
        f"📏 ORB Range: {orb_low:.2f} – {orb_high:.2f}\n"
        f"📈 Prev Close → Last: {prev_close:.2f} → {last_price:.2f} ({move_pct:.1f}%)\n"
        f"📊 RVOL: {rvol:.1f}x · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
        f"🎯 Setup Grade: {grade}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"📣 ORB — {sym}\n"
        f"🕒 {ts}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _alerted_orb.add(sym)
    send_alert("orb", sym, last_price, rvol, extra=extra)


def _maybe_panic_flush(sym: str, closes: List[float], bars: List, prev_close: float, rvol: float, dollar_vol: float):
    if sym in _alerted_flush:
        return
    if not bars or len(bars) < 25:
        return

    last_bar = bars[-1]
    last_price = float(getattr(last_bar, "close", getattr(last_bar, "c", 0)) or 0.0)
    if last_price < INTRADAY_MIN_PRICE:
        return

    if rvol < max(FLUSH_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    if dollar_vol < max(INTRADAY_MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
        return

    move_from_prev = (last_price - prev_close) / prev_close * 100.0 if prev_close > 0 else 0.0
    if move_from_prev > -FLUSH_DROP_MIN_PCT:
        return

    # Last bar drop vs previous bar
    prev_bar = bars[-2]
    prev_close_intraday = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0)) or 0.0)
    if prev_close_intraday <= 0:
        return

    bar_drop_pct = (last_price - prev_close_intraday) / prev_close_intraday * 100.0
    if bar_drop_pct > -FLUSH_BAR_DROP_MIN_PCT:
        return

    # Volume spike on last bar vs recent 20 bars
    vols = [float(getattr(b, "volume", getattr(b, "v", 0)) or 0.0) for b in bars]
    last_vol = vols[-1]
    recent_vols = vols[-21:-1] if len(vols) > 21 else vols[:-1]
    avg_recent = sum(recent_vols) / len(recent_vols) if recent_vols else last_vol
    if avg_recent <= 0:
        return
    if last_vol < avg_recent * FLUSH_BAR_VOL_MULT:
        return

    # Intraday RSI
    intraday_closes = [float(getattr(b, "close", getattr(b, "c", 0)) or 0.0) for b in bars]
    rsi = _compute_rsi(intraday_closes)
    if rsi is None or rsi > FLUSH_MAX_RSI:
        return

    ts = _format_now_est()
    body = (
        f"💥 PANIC FLUSH (Intraday)\n"
        f"📉 Day Move: {move_from_prev:.1f}%\n"
        f"📉 Last Bar Drop: {bar_drop_pct:.1f}%\n"
        f"📊 Last Bar Vol: {int(last_vol):,} (≈ {last_vol / max(avg_recent,1):.1f}× recent)\n"
        f"📉 RSI: {rsi:.1f}\n"
        f"📊 RVOL: {rvol:.1f}x · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"🩸 FLUSH — {sym}\n"
        f"🕒 {ts}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _alerted_flush.add(sym)
    send_alert("panic_flush", sym, last_price, rvol, extra=extra)


def _maybe_momentum_reversal(sym: str, closes: List[float], bars: List, prev_close: float, rvol: float, dollar_vol: float):
    if sym in _alerted_rev:
        return
    if not bars or len(bars) < 25:
        return

    last_bar = bars[-1]
    last_price = float(getattr(last_bar, "close", getattr(last_bar, "c", 0)) or 0.0)
    if last_price < INTRADAY_MIN_PRICE:
        return
    if rvol < max(REV_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    intraday_closes = [float(getattr(b, "close", getattr(b, "c", 0)) or 0.0) for b in bars]
    rsi = _compute_rsi(intraday_closes)
    if rsi is None:
        return

    # Intraday extremes
    intraday_high = max(intraday_closes)
    intraday_low = min(intraday_closes)
    if intraday_high <= 0 or intraday_low <= 0:
        return

    bounce_from_low = (last_price - intraday_low) / intraday_low * 100.0
    fade_from_high = (intraday_high - last_price) / intraday_high * 100.0

    ts = _format_now_est()

    # Oversold bounce
    if rsi <= REV_OVERSOLD_RSI and bounce_from_low >= REV_MIN_BOUNCE_PCT:
        body = (
            f"🔄 INTRADAY REVERSAL (Oversold Bounce)\n"
            f"📉 RSI: {rsi:.1f}\n"
            f"📈 From Low: +{bounce_from_low:.1f}% off intraday low\n"
            f"📊 RVOL: {rvol:.1f}x · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )
        extra = (
            f"📣 REVERSAL — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )
        _alerted_rev.add(sym)
        return send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)

    # Overbought fade
    if rsi >= REV_OVERBOUGHT_RSI and fade_from_high >= REV_MIN_FADE_PCT:
        body = (
            f"🔄 INTRADAY REVERSAL (Overbought Fade)\n"
            f"📈 RSI: {rsi:.1f}\n"
            f"📉 From High: -{fade_from_high:.1f}% off intraday high\n"
            f"📊 RVOL: {rvol:.1f}x · Dollar Vol: ≈ ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )
        extra = (
            f"📣 REVERSAL — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )
        _alerted_rev.add(sym)
        return send_alert("momentum_reversal", sym, last_price, rvol, extra=extra)


# ======================================================
# MAIN ENTRY
# ======================================================

async def run_intraday_flow():
    """
    Unified intraday scanner:
      • ORB breaks
      • Panic flushes
      • Intraday momentum reversals
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[intraday_flow] Missing POLYGON_KEY or client; skipping.")
        return

    if not _in_intraday_window():
        print("[intraday_flow] outside intraday window; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[intraday_flow] empty universe; skipping.")
        return

    trading_day = date.today()
    print(f"[intraday_flow] scanning {len(universe)} tickers…")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Intraday bars
        bars = _fetch_intraday(sym, trading_day)
        if not bars:
            continue

        # Daily stats
        daily_stats = _fetch_daily_stats(sym, trading_day)
        if not daily_stats:
            continue

        prev_close, rvol, dollar_vol = daily_stats

        last_bar = bars[-1]
        last_price = float(getattr(last_bar, "close", getattr(last_bar, "c", 0)) or 0.0)
        if last_price <= 0:
            continue

        if dollar_vol < max(INTRADAY_MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * last_price):
            continue

        closes_daily = []  # not used directly but kept for future extension
        # We’ll use intraday closes only inside the helpers

        try:
            _maybe_orb(sym, closes_daily, bars, prev_close, rvol, dollar_vol)
        except Exception as e:
            print(f"[intraday_flow] ORB error for {sym}: {e}")

        try:
            _maybe_panic_flush(sym, closes_daily, bars, prev_close, rvol, dollar_vol)
        except Exception as e:
            print(f"[intraday_flow] flush error for {sym}: {e}")

        try:
            _maybe_momentum_reversal(sym, closes_daily, bars, prev_close, rvol, dollar_vol)
        except Exception as e:
            print(f"[intraday_flow] reversal error for {sym}: {e}")

    print("[intraday_flow] complete.")