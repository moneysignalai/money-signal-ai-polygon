import os
import time
from datetime import date, timedelta, datetime
from typing import List, Optional, Dict, Any

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
    grade_equity_setup,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    now_est,
)

# NEW: import stats helper
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG ----------------

MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "5"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "4"))
MIN_EARNINGS_DOLLAR_VOL = float(os.getenv("MIN_EARNINGS_DOLLAR_VOL", "1_000_000"))
EARNINGS_NEWS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_NEWS_LOOKBACK_DAYS", "5"))

_CLIENT: Optional[RESTClient] = None

_alert_date: Optional[date] = None
_alerted_symbols: set[str] = set()


# ---------------- INTERNAL HELPERS ----------------

def _get_client() -> Optional[RESTClient]:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY missing.")
        return None
    _CLIENT = RESTClient(POLYGON_KEY)
    return _CLIENT


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_symbols
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_symbols = set()


def _already_alerted(sym: str) -> bool:
    _reset_if_new_day()
    return sym.upper() in _alerted_symbols


def _mark_alerted(sym: str) -> None:
    _reset_if_new_day()
    _alerted_symbols.add(sym.upper())


def _within_earnings_hours() -> bool:
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 7 * 60 <= minutes <= 22 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.9)


def _fetch_earnings_for_symbol(sym: str, earning_date: date) -> Optional[Dict[str, Any]]:
    """
    Placeholder for Polygon's earnings endpoint.
    Currently returns None so only move-based alerts fire.
    """
    return None


# ---------------- MAIN BOT ----------------

async def run_earnings():
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY missing; skipping.")
        record_bot_stats("earnings", 0, 0, 0, 0.0)
        return

    client = _get_client()
    if not client:
        record_bot_stats("earnings", 0, 0, 0, 0.0)
        return

    if not _within_earnings_hours():
        print("[earnings] Outside earnings window; skipping.")
        record_bot_stats("earnings", 0, 0, 0, 0.0)
        return

    BOT_NAME = "earnings"
    start_ts = time.time()

    universe = _get_ticker_universe()
    matches = []
    alerts_sent = 0

    today = date.today()
    today_s = today.isoformat()

    # ---------------- SCAN LOOP ----------------
    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # --- Daily bars ---
        try:
            days = list(
                client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[earnings] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        # prev close
        prev_close_raw = getattr(prev_bar, "close", None) or getattr(prev_bar, "c", None)
        try:
            prev_close = float(prev_close_raw) if prev_close_raw else 0.0
        except:
            prev_close = 0.0
        if prev_close <= 0:
            continue

        # today's open/close
        open_raw = getattr(today_bar, "open", None) or getattr(today_bar, "o", None)
        close_raw = getattr(today_bar, "close", None) or getattr(today_bar, "c", None)

        try:
            open_today = float(open_raw) if open_raw else 0.0
        except:
            open_today = 0.0

        try:
            last_price = float(close_raw) if close_raw else 0.0
        except:
            last_price = 0.0

        if last_price < MIN_EARNINGS_PRICE:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100
        if abs(move_pct) < MIN_EARNINGS_MOVE_PCT:
            continue

        # RVOL
        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = float(sum(d.volume for d in recent)) / len(recent) if recent else float(today_bar.volume)
        rvol = float(today_bar.volume) / avg_vol if avg_vol > 0 else 1.0

        if today_bar.volume < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * today_bar.volume
        if dollar_vol < MIN_EARNINGS_DOLLAR_VOL:
            continue

        matches.append(sym)

        # -------------- EARNINGS FUNDAMENTALS (stub) --------------
        earnings_rec = _fetch_earnings_for_symbol(sym, today)

        _mark_alerted(sym)
        alerts_sent += 1

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)
        gap_pct = (open_today - prev_close) / prev_close * 100 if prev_close > 0 else 0
        intraday_pct = (last_price - open_today) / open_today * 100 if open_today > 0 else 0

        now_str = now_est()
        emoji = "💎"
        bias = "Post-earnings momentum long" if move_pct > 0 else "Post-earnings fade candidate"
        earnings_context = (
            f"Earnings: {earnings_rec.get('report', 'N/A')}" if earnings_rec else "Earnings event (details unavailable)"
        )

        body = (
            f"{emoji} EARNINGS MOVE — {sym}\n"
            f"🕒 {now_str}\n"
            f"💰 Price: ${last_price:.2f}\n"
            f"📊 Move: {move_pct:.1f}% · Gap: {gap_pct:.1f}% · Intraday: {intraday_pct:.1f}%\n"
            f"📦 Vol: {today_bar.volume:,.0f} (≈ ${dollar_vol:,.0f}) · RVOL: {rvol:.1f}x\n"
            f"🎯 Grade: {grade}\n"
            f"📰 {earnings_context}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("earnings", sym, last_price, rvol, extra=body)

    # ---------------- STATS REPORTING ----------------
    run_seconds = time.time() - start_ts

    record_bot_stats(
        BOT_NAME,
        scanned=len(universe),
        matched=len(matches),
        alerts=alerts_sent,
        runtime=run_seconds,
    )

    print(
        f"[earnings] scan complete: scanned={len(universe)} "
        f"matches={len(matches)} alerts={alerts_sent} "
        f"runtime={run_seconds:.2f}s"
    )