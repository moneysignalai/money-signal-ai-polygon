# bots/equity_flow.py
#
# Unified STOCK scanner:
#   • Monster Volume spikes (old volume.py)
#   • Gap Up / Gap Down (old gap.py)
#   • Swing Pullback (old swing_pullback.py)
#
# All three equity signals share:
#   • Same Polygon RESTClient
#   • Same universe resolution
#   • Same daily + intraday data fetch
#
# This cuts HTTP load and makes scanning more "aggressive" without
# hammering Polygon 3x for the same tickers.

import os
from datetime import date, timedelta, datetime
from typing import List, Tuple, Optional

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
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# -------------------------------------------------------------------
# CONFIG (reuses the same ENV VARS you already have in volume / gap /
# swing_pullback so behaviour stays consistent)
# -------------------------------------------------------------------

# Volume / monster-bar thresholds (from volume.py)
MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "2000000"))
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "12000000"))
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))
MIN_VOLUME_RVOL = float(os.getenv("VOLUME_MIN_RVOL", "1.8"))

# Gap thresholds (from gap.py)
MIN_GAP_PRICE = float(os.getenv("MIN_GAP_PRICE", "3.0"))
MIN_GAP_PCT = float(os.getenv("MIN_GAP_PCT", "3.0"))
MIN_GAP_RVOL = float(os.getenv("MIN_GAP_RVOL", "1.5"))
MIN_GAP_DOLLAR_VOL = float(os.getenv("MIN_GAP_DOLLAR_VOL", "5000000"))
GAP_SCAN_END_MIN = int(os.getenv("GAP_SCAN_END_MIN", str(11 * 60)))  # default 11:00 ET

# Swing pullback thresholds (from swing_pullback.py)
MIN_PRICE = float(os.getenv("PULLBACK_MIN_PRICE", "10.0"))
MAX_PRICE = float(os.getenv("PULLBACK_MAX_PRICE", "200.0"))
MIN_DOLLAR_VOL_PULLBACK = float(os.getenv("PULLBACK_MIN_DOLLAR_VOL", "30000000"))
MIN_RVOL_PULLBACK = float(os.getenv("PULLBACK_MIN_RVOL", "2.0"))
MAX_PULLBACK_PCT = float(os.getenv("PULLBACK_MAX_PULLBACK_PCT", "15.0"))
MIN_PULLBACK_PCT = float(os.getenv("PULLBACK_MIN_PULLBACK_PCT", "3.0"))
MAX_RED_DAYS = int(os.getenv("PULLBACK_MAX_RED_DAYS", "3"))
LOOKBACK_DAYS = int(os.getenv("PULLBACK_LOOKBACK_DAYS", "60"))

# Universe control
EQUITY_FLOW_MAX_UNIVERSE = int(os.getenv("EQUITY_FLOW_MAX_UNIVERSE", "200"))
EQUITY_FLOW_TICKER_UNIVERSE = os.getenv("EQUITY_FLOW_TICKER_UNIVERSE")

# Per-day de-duplication per-signal
_alert_date: Optional[date] = None
_alerted_volume: set = set()
_alerted_gap: set = set()
_alerted_pullback: set = set()


# -------------------------------------------------------------------
# CORE HELPERS
# -------------------------------------------------------------------

def _reset_if_new_day() -> None:
    global _alert_date, _alerted_volume, _alerted_gap, _alerted_pullback
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_volume = set()
        _alerted_gap = set()
        _alerted_pullback = set()


def _format_now_est() -> str:
    """
    Make a nice EST timestamp, regardless of whether shared.now_est()
    returns a datetime or a string.
    """
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except Exception:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def _in_rth_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins <= (16 * 60)  # 09:30–16:00 ET


def _in_gap_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins <= GAP_SCAN_END_MIN


def _get_universe() -> List[str]:
    """
    Universe priority:
      1) EQUITY_FLOW_TICKER_UNIVERSE env
      2) TICKER_UNIVERSE env
      3) Dynamic top-volume universe (shared)
    """
    if EQUITY_FLOW_TICKER_UNIVERSE:
        return [s.strip().upper() for s in EQUITY_FLOW_TICKER_UNIVERSE.split(",") if s.strip()]

    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]

    return get_dynamic_top_volume_universe(
        max_tickers=EQUITY_FLOW_MAX_UNIVERSE,
        volume_coverage=0.95,
    )


def _fetch_daily_history(sym: str, trading_day: date) -> List:
    """
    Fetch enough daily bars for ALL three strategies:
      • Volume RVOL (≈ 30 days)
      • Gap RVOL (≈ 40 days)
      • Swing pullback SMA+trend (LOOKBACK_DAYS + safety margin)
    """
    if not _client:
        return []

    # Safety padding
    days_back = max(LOOKBACK_DAYS + 30, 60)
    start = (trading_day - timedelta(days=days_back)).isoformat()
    end = trading_day.isoformat()

    try:
        daily = list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=days_back + 5,
                sort="asc",
            )
        )
        return daily
    except Exception as e:
        print(f"[equity_flow] daily agg error for {sym}: {e}")
        return []


def _fetch_intraday(sym: str, trading_day: date) -> List:
    """
    Minute bars for monster-volume detection.
    Restricted to RTH.
    """
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
        print(f"[equity_flow] intraday agg error for {sym}: {e}")
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


def _compute_day_stats(sym: str, daily: List) -> Optional[dict]:
    """
    Compute shared stats used by multiple strategies:
      - prev_close
      - open_today
      - high/low/last
      - vol_today
      - rvol_20
      - dollar_vol
      - closes list (for SMAs, swing high)
    """
    if len(daily) < 2:
        return None

    today_bar = daily[-1]
    prev_bar = daily[-2]

    try:
        prev_close = float(getattr(prev_bar, "close", getattr(prev_bar, "c", 0)))
        open_today = float(getattr(today_bar, "open", getattr(today_bar, "o", 0)))
        day_high = float(getattr(today_bar, "high", getattr(today_bar, "h", 0)))
        day_low = float(getattr(today_bar, "low", getattr(today_bar, "l", 0)))
        last_price = float(getattr(today_bar, "close", getattr(today_bar, "c", 0)))
        vol_today = float(getattr(today_bar, "volume", getattr(today_bar, "v", 0)))
    except Exception:
        return None

    if last_price <= 0 or prev_close <= 0:
        return None

    closes = [float(getattr(d, "close", getattr(d, "c", 0))) for d in daily]
    vols_hist = [float(getattr(d, "volume", getattr(d, "v", 0))) for d in daily[:-1]]
    recent_vols = vols_hist[-20:] if len(vols_hist) > 20 else vols_hist

    if recent_vols:
        avg_vol = sum(recent_vols) / len(recent_vols)
    else:
        avg_vol = vol_today

    rvol = vol_today / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * vol_today

    return {
        "prev_close": prev_close,
        "open_today": open_today,
        "last_price": last_price,
        "day_low": day_low,
        "day_high": day_high,
        "vol_today": vol_today,
        "rvol": rvol,
        "dollar_vol": dollar_vol,
        "closes": closes,
    }


def _find_monster_bar(bars: List, last_price: float) -> Tuple[bool, float]:
    """
    Look for a "monster" volume bar intraday relative to others.

    Returns (found, monster_bar_vol).
    """
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


def _sma(values: List[float], window: int) -> List[float]:
    if len(values) < window:
        return []
    out: List[float] = []
    for i in range(window - 1, len(values)):
        window_vals = values[i - window + 1 : i + 1]
        out.append(sum(window_vals) / float(window))
    return out

#------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
record_bot_stats(
    "equity_flow",
    scanned=len(universe),
    matched=len(matches),
    alerts=alerts_sent,
    runtime=run_seconds,
)

# -------------------------------------------------------------------
# STRATEGY LOGIC (uses shared stats so we only compute once)
# -------------------------------------------------------------------

def _maybe_alert_volume(sym: str, stats: dict, intraday_bars: List) -> None:
    if sym in _alerted_volume:
        return

    last_price = stats["last_price"]
    rvol = stats["rvol"]
    day_vol = stats["vol_today"]
    dollar_vol = stats["dollar_vol"]

    if last_price < MIN_MONSTER_PRICE:
        return

    # RVOL gate for volume bot: use per-bot MIN_VOLUME_RVOL but also respect global MIN_RVOL_GLOBAL
    if rvol < max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL):
        return
    if day_vol < MIN_VOLUME_GLOBAL:
        return
    if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
        return

    if not intraday_bars:
        return

    found, monster_bar_vol = _find_monster_bar(intraday_bars, last_price)
    if not found:
        return

    prev_close = stats["prev_close"]
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

    time_str = _format_now_est()
    extra = (
        f"📣 VOLUME — {sym}\n"
        f"🕒 {time_str}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _alerted_volume.add(sym)
    send_alert("volume", sym, last_price, rvol, extra=extra)


def _maybe_alert_gap(sym: str, stats: dict) -> None:
    if sym in _alerted_gap:
        return
    if not _in_gap_window():
        return

    last_price = stats["last_price"]
    prev_close = stats["prev_close"]
    open_today = stats["open_today"]
    day_low = stats["day_low"]
    day_high = stats["day_high"]
    vol_today = stats["vol_today"]
    rvol = stats["rvol"]
    dollar_vol = stats["dollar_vol"]

    if last_price < MIN_GAP_PRICE:
        return

    gap_pct = (open_today - prev_close) / prev_close * 100.0
    if abs(gap_pct) < MIN_GAP_PCT:
        return

    intraday_pct = (last_price - open_today) / open_today * 100.0
    total_move_pct = (last_price - prev_close) / prev_close * 100.0

    # RVOL gate for gaps: max of bot-specific and global
    if rvol < max(MIN_GAP_RVOL, MIN_RVOL_GLOBAL):
        return

    if vol_today < MIN_VOLUME_GLOBAL:
        return

    if dollar_vol < MIN_GAP_DOLLAR_VOL:
        return

    direction = "Gap Up" if gap_pct > 0 else "Gap Down"
    emoji = "🚀" if gap_pct > 0 else "🩸"

    grade = grade_equity_setup(total_move_pct, rvol, dollar_vol)
    if gap_pct > 0:
        if intraday_pct > 0:
            bias = "Gap-and-go strength intraday"
        elif intraday_pct < 0:
            bias = "Gap fading intraday"
        else:
            bias = "Holding the gap so far"
    else:
        if intraday_pct < 0:
            bias = "Gap-down continuation lower"
        elif intraday_pct > 0:
            bias = "Gap-down bounce attempt"
        else:
            bias = "Holding the downside gap so far"

    body = (
        f"{emoji} {direction}: {gap_pct:.1f}% vs prior close\n"
        f"📈 Prev Close: ${prev_close:.2f} → Open: ${open_today:.2f} → Last: ${last_price:.2f}\n"
        f"📊 Intraday from open: {intraday_pct:.1f}% · Total move: {total_move_pct:.1f}%\n"
        f"📏 Day Range: Low ${day_low:.2f} – High ${day_high:.2f}\n"
        f"📦 Day Volume: {int(vol_today):,}\n"
        f"🎯 Setup Grade: {grade}\n"
        f"📌 Bias: {bias}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    time_str = _format_now_est()
    extra = (
        f"📣 GAP — {sym}\n"
        f"🕒 {time_str}\n"
        f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _alerted_gap.add(sym)
    send_alert("gap", sym, last_price, rvol, extra=extra)


def _maybe_alert_pullback(sym: str, stats: dict, daily: List) -> None:
    if sym in _alerted_pullback:
        return

    last_price = stats["last_price"]
    prev_close = stats["prev_close"]
    day_vol = stats["vol_today"]
    dollar_vol = stats["dollar_vol"]
    rvol = stats["rvol"]
    closes = stats["closes"]

    # Basic price and liquidity filters
    if last_price < MIN_PRICE or last_price > MAX_PRICE:
        return

    # Liquidity: either explicit pullback dollar-vol threshold OR scaled by MIN_VOLUME_GLOBAL
    min_dol = max(MIN_DOLLAR_VOL_PULLBACK, MIN_VOLUME_GLOBAL * last_price)
    if dollar_vol < min_dol:
        return

    # RVOL gate for pullback: respect global + per-bot
    if rvol < max(MIN_RVOL_GLOBAL, MIN_RVOL_PULLBACK):
        return

    # Trend: 20 SMA > 50 SMA, price above 50SMA
    sma20_series = _sma(closes, 20)
    sma50_series = _sma(closes, 50)
    if not sma20_series or not sma50_series:
        return

    sma20 = sma20_series[-1]
    sma50 = sma50_series[-1]

    if sma20 <= sma50:
        return
    if last_price <= sma50:
        return

    # Recent red days count (using last 1..N closes)
    recent_closes = closes[-(MAX_RED_DAYS + 5) :]
    red_days = 0
    for i in range(1, len(recent_closes)):
        if recent_closes[i] < recent_closes[i - 1]:
            red_days += 1

    if red_days == 0 or red_days > MAX_RED_DAYS:
        return

    # Pullback from recent swing high
    recent_window = closes[-20:]
    if not recent_window:
        return

    swing_high = max(recent_window)
    if swing_high <= 0:
        return

    pullback_pct = (swing_high - last_price) / swing_high * 100.0
    if pullback_pct < MIN_PULLBACK_PCT or pullback_pct > MAX_PULLBACK_PCT:
        return

    move_pct = (last_price / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    grade = grade_equity_setup(move_pct, rvol, dollar_vol)

    timestamp = _format_now_est()
    extra = (
        f"📈 SWING PULLBACK — {sym}\n"
        f"🕒 {timestamp}\n"
        f"💰 ${last_price:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"📌 Strong uptrend: 20 SMA {sma20:.2f} > 50 SMA {sma50:.2f}\n"
        f"📉 Recent pullback: {red_days} red days, ~{pullback_pct:.1f}% from high\n"
        f"📊 Day Move: {move_pct:.1f}% · Volume: {int(day_vol):,}\n"
        f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
        f"🎯 Setup Grade: {grade} · Bias: LONG DIP-BUY\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    _alerted_pullback.add(sym)
    send_alert("swing_pullback", sym, last_price, rvol, extra=extra)


# -------------------------------------------------------------------
# MAIN ENTRYPOINT
# -------------------------------------------------------------------

async def run_equity_flow() -> None:
    """
    Unified STOCK scanner:
      • Volume monster bars
      • Gap up / gap down
      • Swing pullback
    All in ONE pass over the universe, sharing data + client.
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[equity_flow] missing POLYGON_KEY or client; skipping.")
        return

    if not _in_rth_window():
        print("[equity_flow] outside RTH; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[equity_flow] empty universe; skipping.")
        return

    trading_day = date.today()
    print(f"[equity_flow] scanning {len(universe)} symbols for volume/gap/pullback on {trading_day}")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Fetch daily history once
        daily = _fetch_daily_history(sym, trading_day)
        if not daily:
            continue

        stats = _compute_day_stats(sym, daily)
        if not stats:
            continue

        # Intraday only if we MIGHT trigger volume-pings
        intraday = _fetch_intraday(sym, trading_day)

        # Try each strategy; any/all may fire
        try:
            _maybe_alert_volume(sym, stats, intraday)
        except Exception as e:
            print(f"[equity_flow] volume-logic error for {sym}: {e}")

        try:
            _maybe_alert_gap(sym, stats)
        except Exception as e:
            print(f"[equity_flow] gap-logic error for {sym}: {e}")

        try:
            _maybe_alert_pullback(sym, stats, daily)
        except Exception as e:
            print(f"[equity_flow] pullback-logic error for {sym}: {e}")

    print("[equity_flow] scan complete.")
