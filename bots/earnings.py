import os
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

eastern = pytz.timezone("US/Eastern")

MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "5"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "4"))
MIN_EARNINGS_DOLLAR_VOL = float(os.getenv("MIN_EARNINGS_DOLLAR_VOL", "1_000_000"))
EARNINGS_NEWS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_NEWS_LOOKBACK_DAYS", "5"))

_CLIENT: Optional[RESTClient] = None

_alert_date: Optional[date] = None
_alerted_symbols: set[str] = set()


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
    """Check if we've already sent an earnings alert for this symbol today."""
    _reset_if_new_day()
    return sym.upper() in _alerted_symbols


def _mark_alerted(sym: str) -> None:
    """Mark a symbol as alerted for the current day."""
    _reset_if_new_day()
    _alerted_symbols.add(sym.upper())


def _within_earnings_hours() -> bool:
    """
    Only allow earnings alerts between 07:00 and 22:00 EST.

    This prevents overnight spam on the same daily bar.
    """
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 7 * 60 <= minutes <= 22 * 60


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    # dynamic top volume universe
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.9)


def _fetch_earnings_for_symbol(sym: str, earning_date: date) -> Optional[Dict[str, Any]]:
    """
    Placeholder for integrating Polygon's /vX/reference/financials or /events/earnings.
    For now, this is stubbed; you can hook your own earnings API here.
    """
    # TODO: integrate real earnings fundamentals endpoint if desired.
    return None


async def run_earnings():
    """
    Earnings move / RVOL bot.

    Idea:
      • Scan a liquid universe
      • Look for big % move + high RVOL + large dollar volume
      • Confirm there is actually an earnings event today (stubbed for now)
    """
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY missing; skipping.")
        return

    client = _get_client()
    if not client:
        return

    if not _within_earnings_hours():
        print("[earnings] Outside earnings window; skipping.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            continue

        # --- Daily bars: move, RVOL, volume, gap ---

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

        # Safely extract previous close / open; Polygon can occasionally return None
        prev_close_raw = getattr(prev_bar, "close", None) or getattr(prev_bar, "c", None)
        try:
            prev_close = float(prev_close_raw) if prev_close_raw is not None else 0.0
        except (TypeError, ValueError):
            prev_close = 0.0
        if prev_close <= 0:
            continue

        open_raw = getattr(today_bar, "open", None) or getattr(today_bar, "o", None)
        close_raw = getattr(today_bar, "close", None) or getattr(today_bar, "c", None)
        try:
            open_today = float(open_raw) if open_raw is not None else 0.0
        except (TypeError, ValueError):
            open_today = 0.0
        try:
            last_price = float(close_raw) if close_raw is not None else 0.0
        except (TypeError, ValueError):
            last_price = 0.0

        if last_price < MIN_EARNINGS_PRICE:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0  # prev_close guarded above
        if abs(move_pct) < MIN_EARNINGS_MOVE_PCT:
            continue

        # RVOL
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        if avg_vol > 0:
            rvol = float(today_bar.volume) / avg_vol
        else:
            rvol = 1.0

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * vol_today
        if dollar_vol < MIN_EARNINGS_DOLLAR_VOL:
            continue

        # Gap & intraday stats
        gap_pct = (open_today - prev_close) / prev_close * 100.0  # guarded prev_close/open_today
        intraday_pct = (
            (last_price - open_today) / open_today * 100.0
            if open_today > 0
            else 0.0
        )

        #------------SCANNER FOR STATUS_REPORT.PY BOT-----------------
record_bot_stats(
    "trend_flow",
    scanned=len(universe),
    matched=len(matches),
    alerts=alerts_sent,
    runtime=run_seconds,
)

        # --- Confirm actual earnings event + enrich with fundamentals ---
        earnings_rec = _fetch_earnings_for_symbol(sym, today)
        if not earnings_rec:
            # Big mover, but not actually an earnings event → let other bots handle it
            continue

        # Parse earnings fundamentals
        earn_date = earnings_rec.get("date")
        earn_time = earnings_rec.get("time")
        session_label = f"{earn_date} {earn_time}" if earn_date or earn_time else "Earnings"

        est_eps = earnings_rec.get("estimated_eps")
        prev_eps = earnings_rec.get("previous_eps")
        est_rev = earnings_rec.get("estimated_revenue")
        prev_rev = earnings_rec.get("previous_revenue")

        eps_surprise_pct = earnings_rec.get("eps_surprise_percent")
        rev_surprise_pct = earnings_rec.get("revenue_surprise_percent")

        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        now_str = now_est()
        emoji = "💎"
        bias = (
            "Post-earnings momentum long"
            if move_pct > 0
            else "Post-earnings fade / short candidate on weakness"
        )

        fundamentals_lines = []
        if est_eps is not None or prev_eps is not None:
            fundamentals_lines.append(f"EPS: est {est_eps} vs prev {prev_eps}")
        if est_rev is not None or prev_rev is not None:
            fundamentals_lines.append(f"Rev: est {est_rev} vs prev {prev_rev}")
        if eps_surprise_pct is not None:
            fundamentals_lines.append(f"EPS surprise: {eps_surprise_pct}%")
        if rev_surprise_pct is not None:
            fundamentals_lines.append(f"Rev surprise: {rev_surprise_pct}%")

        fundamentals_block = "\n".join(f"• {line}" for line in fundamentals_lines) if fundamentals_lines else "• (Fundamentals stub)"

        body = (
            f"{emoji} EARNINGS MOVE — {sym}\n"
            f"🕒 {now_str} ({session_label})\n"
            f"💰 Price: ${last_price:.2f} (prev close ${prev_close:.2f})\n"
            f"📊 Move: {move_pct:.1f}% · Gap: {gap_pct:.1f}% · Intraday: {intraday_pct:.1f}%\n"
            f"📦 Vol: {vol_today:,.0f} (≈ ${dollar_vol:,.0f}) · RVOL: {rvol:.1f}x\n"
            f"🎯 Grade: {grade}\n"
            f"🧠 Bias: {bias}\n"
            "────────────\n"
            f"📊 Earnings snapshot:\n"
            f"{fundamentals_block}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)
        send_alert("earnings", sym, last_price, rvol, extra=body)
