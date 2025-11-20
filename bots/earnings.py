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
    now_est,  # NEW: for pretty timestamp line
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

# --- News capability flags ---

_news_capability_checked = False
_news_supported = False


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

def _ensure_news_capability_checked():
    """
    Detect once whether the current RESTClient supports list_news.
    If not, we disable the news requirement to avoid noisy errors.
    """
    global _news_capability_checked, _news_supported, REQUIRE_EARNINGS_NEWS

    if _news_capability_checked:
        return

    _news_capability_checked = True

    if not _client:
        _news_supported = False
        # If we don't even have a client, there's no point in requiring news
        if REQUIRE_EARNINGS_NEWS:
            print("[earnings] No client available; disabling REQUIRE_EARNINGS_NEWS.")
            REQUIRE_EARNINGS_NEWS = False
        return

    if hasattr(_client, "list_news"):
        _news_supported = True
    else:
        _news_supported = False
        if REQUIRE_EARNINGS_NEWS:
            print(
                "[earnings] Client has no list_news(). "
                "Disabling REQUIRE_EARNINGS_NEWS to avoid errors."
            )
            REQUIRE_EARNINGS_NEWS = False


def _has_recent_earnings_news(sym: str, today: date) -> bool:
    """
    Optionally require a recent 'earnings-style' news headline
    to reduce false positives.

    If the client does NOT support list_news (older polygon client),
    we simply return True and skip the filter to avoid attribute errors.
    """
    _ensure_news_capability_checked()

    # If we've turned off the requirement (either via env or capability), treat as pass.
    if not REQUIRE_EARNINGS_NEWS:
        return True

    if not _client or not _news_supported:
        # This should not happen after _ensure_news_capability_checked, but guard anyway.
        return True

    try:
        from_dt = today - timedelta(days=EARNINGS_NEWS_LOOKBACK_DAYS)
        from_iso = datetime(from_dt.year, from_dt.month, from_dt.day, tzinfo=pytz.UTC).isoformat()

        # Newer polygon clients: list_news
        news_items = list(
            _client.list_news(
                ticker=sym,
                published_utc_gte=from_iso,
                limit=20,
            )
        )
    except Exception as e:
        print(f"[earnings] news fetch failed for {sym}: {e}")
        # On failure, don't block the signal — just allow it through.
        return True

    if not news_items:
        # If we strictly require news, this will block; if you want more signals,
        # you can set REQUIRE_EARNINGS_NEWS=false in ENV.
        return False

    keywords = ("earnings", "results", "guidance", "quarter", "q1", "q2", "q3", "q4")
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
      • Optional: at least one recent 'earnings' news item, if supported
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
    _ensure_news_capability_checked()

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

        # Optional: require an earnings-style news item (only if supported)
        if not _has_recent_earnings_news(sym, today):
            continue

        # Grade + bias
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)

        if move_pct > 0:
            bias = "Long earnings momentum"
        else:
            bias = "Post-earnings fade / short setup"

        # 🔔 NEW ALERT FORMAT (your requested style)
        extra = (
            f"📣 EARNINGS — {sym}\n"
            f"🕒 {now_est()}\n"
            f"────────────\n"
            f"✅ Earnings Today\n"
            f"📌 Report Time: Today (price reaction scan)\n"
            f"💵 Move: {move_pct:.1f}% vs prior close\n"
            f"📊 Gap: {gap_pct:.1f}% · Intraday: {intraday_pct:.1f}% from open\n"
            f"📦 Volume: {int(vol_today):,} · RVOL {rvol:.1f}x\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)
        send_alert("earnings", sym, last_price, rvol, extra=extra)