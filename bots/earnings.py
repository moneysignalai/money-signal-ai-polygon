import os
from datetime import date, timedelta, datetime
from typing import List

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
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# --- Thresholds & config ---

MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "2.0"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "9.0"))     # % vs prior close
MIN_EARNINGS_RVOL = float(os.getenv("MIN_EARNINGS_RVOL", "3.0"))             # RVOL explosion
MIN_EARNINGS_DOLLAR_VOL = float(os.getenv("MIN_EARNINGS_DOLLAR_VOL", "10000000"))  # $10M+

EARNINGS_NEWS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_NEWS_LOOKBACK_DAYS", "3"))
REQUIRE_EARNINGS_NEWS = os.getenv("REQUIRE_EARNINGS_NEWS", "true").lower() == "true"

# --- Per-day de-dupe state (in-memory) ---

_alerted_date: date | None = None
_alerted_symbols: set[str] = set()


def _reset_if_new_day() -> None:
    """
    Reset the per-day de-dupe set when the calendar day changes.
    """
    global _alerted_date, _alerted_symbols
    today = date.today()
    if _alerted_date != today:
        _alerted_date = today
        _alerted_symbols = set()


def _already_alerted(sym: str) -> bool:
    """
    Check if we've already sent an earnings alert for this symbol today.
    """
    _reset_if_new_day()
    return sym in _alerted_symbols


def _mark_alerted(sym: str) -> None:
    """
    Mark a symbol as alerted for the current day.
    """
    _reset_if_new_day()
    _alerted_symbols.add(sym)


# --- Time window guard ---

def _in_earnings_window() -> bool:
    """
    Only run between 7:00 AM and 10:00 PM EST.
    This prevents overnight spam on the same daily bar.
    """
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 7 * 60 <= minutes <= 22 * 60


# --- Universe helper ---

def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    # dynamic top volume universe (100 tickers, ~90% of volume)
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


# --- Optional earnings-news check ---

def _has_recent_earnings_news(sym: str, today: date) -> bool:
    """
    Optionally require a recent 'earnings-style' news headline
    to reduce false positives.

    You can turn this off with REQUIRE_EARNINGS_NEWS=false.
    """
    if not REQUIRE_EARNINGS_NEWS:
        return True

    if not _client:
        return False

    try:
        from_dt = today - timedelta(days=EARNINGS_NEWS_LOOKBACK_DAYS)
        from_iso = datetime(from_dt.year, from_dt.month, from_dt.day, tzinfo=pytz.UTC).isoformat()
        # Polygon: list_news supports published_utc.gte in query params,
        # but some client versions require "published_utc.gte" in kwargs.
        news_items = list(
            _client.list_news(
                ticker=sym,
                published_utc_gte=from_iso,
                limit=20,
            )
        )
    except Exception as e:
        print(f"[earnings] news fetch failed for {sym}: {e}")
        return False

    if not news_items:
        return False

    keywords = ("earnings", "results", "guidance", "quarter", "Q1", "Q2", "Q3", "Q4")
    for n in news_items:
        title = (getattr(n, "title", "") or "").lower()
        if any(k in title for k in keywords):
            return True

    return False


# --- Main bot ---

async def run_earnings():
    """
    Earnings Move Bot (post-earnings reaction scanner):

      • Price >= MIN_EARNINGS_PRICE
      • |Move vs prior close| >= MIN_EARNINGS_MOVE_PCT
      • Day RVOL >= max(MIN_EARNINGS_RVOL, MIN_RVOL_GLOBAL)
      • Day volume >= MIN_VOLUME_GLOBAL
      • Dollar volume >= MIN_EARNINGS_DOLLAR_VOL
      • Optional: at least one recent 'earnings' news item
      • Only runs between 7:00 AM and 10:00 PM EST
      • Per-symbol per-day de-dupe: each ticker alerts at most once per day
    """
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[earnings] Client not initialized; skipping scan.")
        return
    if not _in_earnings_window():
        print("[earnings] Outside 07:00–22:00 EST window; skipping scan.")
        return

    _reset_if_new_day()

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already_alerted(sym):
            # we already pushed an earnings alert for this name today
            continue

        # --- Daily bars: move, RVOL, volume, gap ---

        try:
            days = list(
                _client.list_aggs(
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

        prev_close = float(prev_bar.close)
        if prev_close <= 0:
            continue

        open_today = float(today_bar.open)
        last_price = float(today_bar.close)

        if last_price < MIN_EARNINGS_PRICE:
            continue

        move_pct = (last_price - prev_close) / prev_close * 100.0
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

        if rvol < max(MIN_EARNINGS_RVOL, MIN_RVOL_GLOBAL):
            continue

        vol_today = float(today_bar.volume)
        if vol_today < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * vol_today
        if dollar_vol < MIN_EARNINGS_DOLLAR_VOL:
            continue

        # Gap & intraday stats
        gap_pct = (open_today - prev_close) / prev_close * 100.0
        intraday_pct = (
            (last_price - open_today) / open_today * 100.0
            if open_today > 0
            else 0.0
        )

        # Optional: require an earnings-style news item
        if not _has_recent_earnings_news(sym, today):
            continue

        # Grade + bias
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        if move_pct > 0:
            bias = "Long earnings momentum"
        else:
            bias = "Post-earnings fade / short setup"

        extra = (
            f"📣 Earnings move: {move_pct:.1f}% today\n"
            f"📈 Prev Close: ${prev_close:.2f} → Open: ${open_today:.2f} → Close: ${last_price:.2f}\n"
            f"📊 Gap: {gap_pct:.1f}% · Intraday: {intraday_pct:.1f}% from open\n"
            f"📦 Volume: {int(vol_today):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        # Mark before sending so even if the process crashes mid-send,
        # we are still conservative about duplicates on restart.
        _mark_alerted(sym)
        send_alert("earnings", sym, last_price, rvol, extra=extra)