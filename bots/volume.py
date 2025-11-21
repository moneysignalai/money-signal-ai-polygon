# bots/volume.py — HYBRID "Monster Volume" Scanner (2025)
#
# Hybrid logic (Option C):
#   • Uses both absolute thresholds (shares / dollar vol)
#   • AND relative spike vs prior 5-min average ("spike ratio")
#   • Still respects RVOL, dollar volume and price filters.
#
# One alert per symbol per day, premium Telegram style.

import os
from datetime import date, timedelta, datetime
from typing import List, Tuple

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
    now_est,  # NOTE: string helper from shared.py
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ------- CONFIG (tunable via ENV) -------

# Absolute “monster” bar thresholds
# (Defaults are already a bit softer than the old v1, but still meaningful.)
MIN_MONSTER_BAR_SHARES = float(os.getenv("MIN_MONSTER_BAR_SHARES", "2000000"))   # was 8,000,000
MIN_MONSTER_DOLLAR_VOL = float(os.getenv("MIN_MONSTER_DOLLAR_VOL", "12000000"))  # was 30,000,000
MIN_MONSTER_PRICE = float(os.getenv("MIN_MONSTER_PRICE", "2.0"))

# Per-symbol RVOL threshold for the day (volume bot specific)
# Slightly softer to allow more names through, but still respects MIN_RVOL_GLOBAL.
MIN_VOLUME_RVOL = float(os.getenv("VOLUME_MIN_RVOL", "1.7"))

# Spike-based logic:
#   spike_ratio = bar_volume / avg(prev_5_bars_volume)
# If this ratio is high enough, we allow a "monster" even with somewhat smaller absolute bar size.
MIN_SPIKE_RATIO = float(os.getenv("VOLUME_MIN_SPIKE_RATIO", "4.0"))
SPIKE_DOLLAR_VOL_FACTOR = float(os.getenv("VOLUME_SPIKE_DOLLAR_VOL_FACTOR", "0.5"))
# e.g. 0.5 * MIN_MONSTER_DOLLAR_VOL

# Per-day de-dupe
_alert_date: date | None = None
_alerted_syms: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_syms
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_syms = set()


def _already_alerted(sym: str) -> bool:
    return sym in _alerted_syms


def _mark(sym: str) -> None:
    _alerted_syms.add(sym)


# ------- RTH WINDOW -------

def _in_volume_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    # 09:30–16:00 ET
    return (9 * 60 + 30) <= mins <= (16 * 60)


# ------- Universe -------

def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


# ------- Intraday Fetch -------

def _fetch_intraday(sym: str, trading_day: date) -> List:
    """
    Fetch all 1-minute bars for the given day and filter to RTH (09:30–16:00 ET).
    Attaches _et (datetime in ET) to each bar for convenience.
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
        print(f"[volume] intraday agg error for {sym}: {e}")
        return []

    filtered = []
    for b in bars:
        ts = getattr(b, "timestamp", getattr(b, "t", None))
        if ts is None:
            continue
        # polygon may return in ms or ns; normalize to seconds
        if ts > 1e12:  # ms → s
            ts = ts / 1000.0
        if ts > 1e12:  # ns → s (very defensive)
            ts = ts / 1_000_000_000.0

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


# ------- RVOL + Day Stats -------

def _compute_rvol_and_day_stats(sym: str, trading_day: date) -> Tuple[float, float, float, float, float, List]:
    """
    Return:
      (rvol, day_vol, last_price, prev_close, dollar_vol, intraday_bars)
    """
    if not _client:
        return 1.0, 0.0, 0.0, 0.0, 0.0, []

    # Intraday minute bars for day volume / last price
    bars = _fetch_intraday(sym, trading_day)
    if not bars:
        return 1.0, 0.0, 0.0, 0.0, 0.0, []

    day_vol = float(sum(getattr(b, "volume", getattr(b, "v", 0)) for b in bars))
    last_price = float(getattr(bars[-1], "close", getattr(bars[-1], "c", 0)) or 0)

    # Daily history for RVOL / prev close
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
        print(f"[volume] daily agg error for {sym}: {e}")
        return 1.0, day_vol, last_price, 0.0, last_price * day_vol, bars

    if not daily:
        return 1.0, day_vol, last_price, 0.0, last_price * day_vol, bars

    d0 = daily[-1]
    if len(daily) >= 2:
        prev_close = float(getattr(daily[-2], "close", getattr(daily[-2], "c", 0)) or 0.0)
    else:
        prev_close = 0.0

    hist = daily[:-1] if len(daily) > 1 else daily
    recent = hist[-20:] if len(hist) > 20 else hist
    if not recent:
        avg_vol = float(getattr(d0, "volume", getattr(d0, "v", 0)) or 0.0)
    else:
        avg_vol = sum(
            float(getattr(d, "volume", getattr(d, "v", 0)) or 0.0) for d in recent
        ) / len(recent)

    rvol = day_vol / avg_vol if avg_vol > 0 else 1.0
    dollar_vol = last_price * day_vol
    return rvol, day_vol, last_price, prev_close, dollar_vol, bars


# ------- Monster / Spike Detection -------

def _find_monster_bar(sym: str, bars: List, last_price: float) -> Tuple[bool, float, float]:
    """
    Look for a "monster" volume bar intraday using a hybrid approach:

      • Absolute condition:
          - max_bar_vol >= MIN_MONSTER_BAR_SHARES
          - max_bar_dollar >= MIN_MONSTER_DOLLAR_VOL

      • OR Spike condition:
          - spike_ratio (bar_vol / avg(prev 5 bars)) >= MIN_SPIKE_RATIO
          - bar_dollar >= SPIKE_DOLLAR_VOL_FACTOR * MIN_MONSTER_DOLLAR_VOL

    Returns:
      (found, monster_bar_vol, spike_ratio)
    """
    if not bars or last_price <= 0:
        return False, 0.0, 0.0

    vols = [float(getattr(b, "volume", getattr(b, "v", 0)) or 0.0) for b in bars]
    if not vols:
        return False, 0.0, 0.0

    max_bar_vol = 0.0
    max_spike_ratio = 0.0

    for i, v in enumerate(vols):
        # compute average of previous up-to-5 bars
        if i == 0:
            avg_prev = 0.0
        else:
            start_idx = max(0, i - 5)
            window = vols[start_idx:i]
            avg_prev = sum(window) / len(window) if window else 0.0

        if avg_prev > 0:
            ratio = v / avg_prev
        else:
            ratio = 0.0

        if v > max_bar_vol:
            max_bar_vol = v
        if ratio > max_spike_ratio:
            max_spike_ratio = ratio

    dollar_bar = max_bar_vol * last_price

    # Absolute monster condition
    hard_cond = (max_bar_vol >= MIN_MONSTER_BAR_SHARES) and (dollar_bar >= MIN_MONSTER_DOLLAR_VOL)

    # Spike-based condition (more aggressive, but still dollar-vol aware)
    spike_dollar_floor = MIN_MONSTER_DOLLAR_VOL * SPIKE_DOLLAR_VOL_FACTOR
    spike_cond = (max_spike_ratio >= MIN_SPIKE_RATIO) and (dollar_bar >= spike_dollar_floor)

    if not (hard_cond or spike_cond):
        return False, max_bar_vol, max_spike_ratio

    return True, max_bar_vol, max_spike_ratio


# ------- MAIN BOT -------

async def run_volume():
    """
    Volume Monster / Spike Bot (Hybrid):

      • Universe:
            - TICKER_UNIVERSE env OR
            - dynamic top-volume universe (shared)
      • Day filters:
            - Price >= MIN_MONSTER_PRICE
            - Day RVOL >= max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL)
            - Day volume >= MIN_VOLUME_GLOBAL
            - Day dollar volume >= MIN_MONSTER_DOLLAR_VOL
      • Intraday conditions (hybrid):
            - Either:
                * max 1-min bar shares >= MIN_MONSTER_BAR_SHARES AND
                * bar-dollar-vol >= MIN_MONSTER_DOLLAR_VOL
              OR:
                * spike_ratio >= MIN_SPIKE_RATIO (vs prior 5-min avg)
                * bar-dollar-vol >= SPIKE_DOLLAR_VOL_FACTOR * MIN_MONSTER_DOLLAR_VOL
      • One alert per symbol per day.
    """
    _reset_if_new_day()

    if not _in_volume_window():
        print("[volume] outside RTH window; skipping.")
        return

    if not POLYGON_KEY or not _client:
        print("[volume] missing POLYGON_KEY or client; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[volume] empty universe; skipping.")
        return

    trading_day = date.today()
    time_str = now_est()  # already a formatted string from shared

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # Compute day stats + get intraday bars
        rvol, day_vol, last_price, prev_close, dollar_vol, bars = _compute_rvol_and_day_stats(sym, trading_day)

        if last_price <= 0 or prev_close <= 0:
            continue
        if last_price < MIN_MONSTER_PRICE:
            continue

        # Softer, bot-specific RVOL gate but still >= global floor
        if rvol < max(MIN_VOLUME_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_MONSTER_DOLLAR_VOL:
            continue

        if not bars:
            continue

        found, monster_bar_vol, spike_ratio = _find_monster_bar(sym, bars, last_price)
        if not found:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
        grade = grade_equity_setup(move_pct, rvol, dollar_vol)
        bias = "Bullish accumulation" if move_pct >= 0 else "Bearish distribution"

        body = (
            f"💥 Monster / Spike Volume Detected\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,} (≈ ${dollar_vol:,.0f} notional)\n"
            f"📦 Biggest 1-min Bar: {int(monster_bar_vol):,} shares "
            f"(≈ ${monster_bar_vol * last_price:,.0f})\n"
            f"📊 RVOL (day): {rvol:.1f}x\n"
            f"⚡ Spike Ratio: {spike_ratio:.1f}x vs prior 5-min avg\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 VOLUME — {sym}\n"
            f"🕒 {time_str}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        send_alert("volume", sym, last_price, rvol, extra=extra)
        _mark(sym)