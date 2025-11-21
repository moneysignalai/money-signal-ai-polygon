# bots/trend_flow.py
#
# Unified Trend Scanner:
#   • Momentum Reversal (oversold bounce / overbought fade)
#   • Trend Rider (trend continuation)
#   • Panic Flush (high-volume selloff)
#
# Eliminates 3 separate files and combines ALL logic into ONE pass over the universe,
# with shared daily data + Polygon client.

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
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ======================================================
# CONFIG — Reuses ALL your existing env vars
# ======================================================

# Momentum Reversal
REV_MIN_PRICE = float(os.getenv("REV_MIN_PRICE", "4.0"))
REV_MIN_RVOL = float(os.getenv("REV_MIN_RVOL", "1.8"))
REV_MAX_RSI = float(os.getenv("REV_MAX_RSI", "35.0"))      # Oversold bounce
REV_MIN_RSI_FADE = float(os.getenv("REV_MIN_RSI_FADE", "70.0"))  # Overbought fade

# Trend Rider
TR_MIN_PRICE = float(os.getenv("TR_MIN_PRICE", "5.0"))
TR_MIN_RVOL = float(os.getenv("TR_MIN_RVOL", "1.5"))
TR_USE_EMA9 = True
TR_USE_EMA21 = True
TR_USE_SMA50 = True

# Panic Flush
PF_MIN_PRICE = float(os.getenv("PF_MIN_PRICE", "3.0"))
PF_MIN_RVOL = float(os.getenv("PF_MIN_RVOL", "2.0"))
PF_DROP_MIN_PCT = float(os.getenv("PF_DROP_MIN_PCT", "5.0"))
PF_MAX_RSI = float(os.getenv("PF_MAX_RSI", "30.0"))

# Universe control
TREND_FLOW_MAX_UNIVERSE = int(os.getenv("TREND_FLOW_MAX_UNIVERSE", "200"))
TREND_FLOW_TICKER_UNIVERSE = os.getenv("TREND_FLOW_TICKER_UNIVERSE")

# De-dupe
_alert_date = None
_alerted_rev = set()
_alerted_trend = set()
_alerted_flush = set()


# ======================================================
# DAY RESET
# ======================================================

def _reset_if_new_day():
    global _alert_date, _alerted_rev, _alerted_trend, _alerted_flush
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_rev = set()
        _alerted_trend = set()
        _alerted_flush = set()


def _format_now_est():
    try:
        ts = now_est()
        if isinstance(ts, str):
            return ts
        return ts.strftime("%I:%M %p EST · %b %d").lstrip("0")
    except:
        return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ======================================================
# HELPERS
# ======================================================

def _get_universe() -> List[str]:
    if TREND_FLOW_TICKER_UNIVERSE:
        return [s.strip().upper() for s in TREND_FLOW_TICKER_UNIVERSE.split(",") if s.strip()]

    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]

    return get_dynamic_top_volume_universe(
        max_tickers=TREND_FLOW_MAX_UNIVERSE,
        volume_coverage=0.95,
    )


def _fetch_daily(sym: str, trading_day: date) -> List:
    if not _client:
        return []
    start = (trading_day - timedelta(days=120)).isoformat()
    end = trading_day.isoformat()

    try:
        return list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=150,
                sort="asc",
            )
        )
    except Exception as e:
        print(f"[trend_flow] daily error for {sym}: {e}")
        return []


def _compute_rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 2:
        return None

    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))

    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    k = 2 / (window + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


# ======================================================
# STRATEGIES
# ======================================================

def _maybe_momentum_reversal(sym: str, stats, closes: List[float]):
    if sym in _alerted_rev:
        return

    last = stats["last"]
    rvol = stats["rvol"]
    if last < REV_MIN_PRICE:
        return
    if rvol < max(REV_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    rsi = _compute_rsi(closes)
    if not rsi:
        return

    move_pct = stats["move"]
    dollar_vol = stats["dollar_vol"]

    # ---- OVERSOLD BOUNCE ----
    if rsi <= REV_MAX_RSI:
        body = (
            f"🔄 MOMENTUM REVERSAL (Oversold)\n"
            f"📉 RSI: {rsi:.1f}\n"
            f"📈 Move: {move_pct:.1f}%\n"
            f"📦 Dollar Vol: ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        ts = _format_now_est()
        extra = (
            f"📣 REVERSAL — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last:.2f} · RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )
        _alerted_rev.add(sym)
        return send_alert("momentum_reversal", sym, last, rvol, extra=extra)

    # ---- OVERBOUGHT FADE ----
    if rsi >= REV_MIN_RSI_FADE:
        body = (
            f"🔄 MOMENTUM REVERSAL (Overbought Fade)\n"
            f"📈 RSI: {rsi:.1f}\n"
            f"📉 Move: {move_pct:.1f}%\n"
            f"📦 Dollar Vol: ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )
        ts = _format_now_est()
        extra = (
            f"📣 REVERSAL — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${last:.2f} · RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )
        _alerted_rev.add(sym)
        return send_alert("momentum_reversal", sym, last, rvol, extra=extra)


def _maybe_trend_rider(sym: str, stats, closes: List[float]):
    if sym in _alerted_trend:
        return

    last = stats["last"]
    rvol = stats["rvol"]
    if last < TR_MIN_PRICE:
        return
    if rvol < max(TR_MIN_RVOL, MIN_RVOL_GLOBAL):
        return

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

    if not ema9 or not ema21 or not sma50:
        return

    if not (ema9 > ema21 > sma50):
        return
    if not (last > ema9):
        return

    ts = _format_now_est()
    body = (
        f"📈 TREND RIDER (Continuation)\n"
        f"EMA9: {ema9:.2f}, EMA21: {ema21:.2f}, SMA50: {sma50:.2f}\n"
        f"RVOL: {rvol:.1f}x\n"
        f"🔗 Chart: {chart_link(sym)}"
    )
    extra = (
        f"⚡ TREND — {sym}\n"
        f"🕒 {ts}\n"
        f"💰 ${last:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )
    _alerted_trend.add(sym)
    send_alert("trend_rider", sym, last, rvol, extra=extra)


def _maybe_panic_flush(sym: str, stats, closes: List[float]):
    if sym in _alerted_flush:
        return

    last = stats["last"]
    rvol = stats["rvol"]
    move_pct = stats["move"]

    if last < PF_MIN_PRICE:
        return
    if rvol < max(PF_MIN_RVOL, MIN_RVOL_GLOBAL):
        return
    if move_pct > -PF_DROP_MIN_PCT:
        return

    rsi = _compute_rsi(closes)
    if not rsi or rsi > PF_MAX_RSI:
        return

    ts = _format_now_est()
    body = (
        f"💥 PANIC FLUSH\n"
        f"📉 Drop: {move_pct:.1f}%\n"
        f"📉 RSI: {rsi:.1f}\n"
        f"📊 RVOL: {rvol:.1f}\n"
        f"🔗 Chart: {chart_link(sym)}"
    )

    extra = (
        f"🩸 FLUSH — {sym}\n"
        f"🕒 {ts}\n"
        f"💰 ${last:.2f} · RVOL {rvol:.1f}x\n"
        "────────────\n"
        f"{body}"
    )

    _alerted_flush.add(sym)
    send_alert("panic_flush", sym, last, rvol, extra=extra)


# ======================================================
# MAIN
# ======================================================

async def run_trend_flow():
    """
    Unified Trend Scanner:
      - Momentum Reversal
      - Trend Rider
      - Panic Flush
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[trend_flow] Missing API key or client.")
        return

    universe = _get_universe()
    if not universe:
        print("[trend_flow] Empty universe.")
        return

    today = date.today()
    print(f"[trend_flow] scanning {len(universe)} tickers…")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        daily = _fetch_daily(sym, today)
        if len(daily) < 20:
            continue

        # Stats
        today_bar = daily[-1]
        prev_bar = daily[-2]

        last = float(getattr(today_bar, "close", 0))
        prev_close = float(getattr(prev_bar, "close", 0))
        vol = float(getattr(today_bar, "volume", 0))

        closes = [float(getattr(d, "close", 0)) for d in daily]

        if prev_close <= 0:
            continue

        move_pct = (last - prev_close) / prev_close * 100
        dollar_vol = last * vol

        # RVOL calc
        hist_vols = [float(getattr(d, "volume", 0)) for d in daily[:-1]]
        recent = hist_vols[-20:] if len(hist_vols) > 20 else hist_vols
        avg_vol = sum(recent) / len(recent) if recent else vol
        rvol = vol / avg_vol if avg_vol > 0 else 1.0

        stats = {
            "last": last,
            "prev_close": prev_close,
            "move": move_pct,
            "rvol": rvol,
            "vol": vol,
            "dollar_vol": dollar_vol,
        }

        # Fire all strategies
        try: _maybe_momentum_reversal(sym, stats, closes)
        except Exception as e: print(f"[trend_flow] rev error for {sym}: {e}")

        try: _maybe_trend_rider(sym, stats, closes)
        except Exception as e: print(f"[trend_flow] trend error for {sym}: {e}")

        try: _maybe_panic_flush(sym, stats, closes)
        except Exception as e: print(f"[trend_flow] flush error for {sym}: {e}")

    print("[trend_flow] complete.")