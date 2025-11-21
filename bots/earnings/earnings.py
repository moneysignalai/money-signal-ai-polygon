import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple

import pytz
import requests

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient  # type: ignore

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
    now_est,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# --- Massive / Benzinga endpoints for earnings + news ---

MASSIVE_BASE_URL = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com")
BENZINGA_EARNINGS_URL = f"{MASSIVE_BASE_URL}/benzinga/v1/earnings"
BENZINGA_NEWS_URL = f"{MASSIVE_BASE_URL}/benzinga/v1/news"

# --- Thresholds & config (price-reaction filters) ---

MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "2.0"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "9.0"))          # % vs prior close
MIN_EARNINGS_RVOL = float(os.getenv("MIN_EARNINGS_RVOL", "3.0"))                  # RVOL explosion
MIN_EARNINGS_DOLLAR_VOL = float(os.getenv("MIN_EARNINGS_DOLLAR_VOL", "10000000")) # $10M+

# Benzinga earnings filters
MIN_EARNINGS_IMPORTANCE = int(os.getenv("MIN_EARNINGS_IMPORTANCE", "2"))          # 0–5, skip junk
EARNINGS_NEWS_LOOKBACK_DAYS = int(os.getenv("EARNINGS_NEWS_LOOKBACK_DAYS", "3"))

# --- Per-day de-dupe state (in-memory) ---

_alerted_date: date | None = None
_alerted_symbols: set[str] = set()


def _reset_if_new_day() -> None:
    """Reset the per-day de-dupe set when the calendar day changes."""
    global _alerted_date, _alerted_symbols
    today = date.today()
    if _alerted_date != today:
        _alerted_date = today
        _alerted_symbols = set()


def _already_alerted(sym: str) -> bool:
    """Check if we've already sent an earnings alert for this symbol today."""
    _reset_if_new_day()
    return sym.upper() in _alerted_symbols


def _mark_alerted(sym: str) -> None:
    """Mark a symbol as alerted for the current day."""
    _reset_if_new_day()
    _alerted_symbols.add(sym.upper())


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


# --- Benzinga earnings + news helpers ---

def _classify_session(earn_date: Optional[str], earn_time: Optional[str]) -> str:
    """
    Map earnings time to nice label:
      - Before Market Open (Pre-Market)
      - During Market Hours
      - After Market Close (Post-Market)
      - TBD
    Benzinga 'time' is HH:MM:SS in UTC.
    """
    if not earn_date or not earn_time:
        return "TBD"

    try:
        dt_utc = datetime.strptime(f"{earn_date} {earn_time}", "%Y-%m-%d %H:%M:%S")
        dt_utc = dt_utc.replace(tzinfo=pytz.UTC)
        dt_et = dt_utc.astimezone(eastern)
    except Exception:
        return "TBD"

    h, m = dt_et.hour, dt_et.minute
    mins = h * 60 + m

    if mins < 9 * 60 + 30:
        return "Before Market Open (Pre-Market)"
    elif mins <= 16 * 60:
        return "During Market Hours"
    else:
        return "After Market Close (Post-Market)"


def _fetch_earnings_for_symbol(sym: str, today: date) -> Optional[dict]:
    """
    Fetch today's Benzinga earnings record for a specific symbol.
    Returns a single dict (most relevant record) or None if not an earnings name.
    """
    if not POLYGON_KEY:
        return None

    params = {
        "ticker": sym.upper(),
        "date": today.isoformat(),
        "limit": 5,
        "sort": "time.asc",
        "order": "asc",
        "apiKey": POLYGON_KEY,
    }

    try:
        r = requests.get(BENZINGA_EARNINGS_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", []) or []
    except Exception as e:
        print(f"[earnings] Benzinga earnings fetch failed for {sym}: {e}")
        return None

    # Filter by importance if present
    filtered: List[dict] = []
    for rec in results:
        imp = rec.get("importance")
        if imp is not None and imp < MIN_EARNINGS_IMPORTANCE:
            continue
        filtered.append(rec)

    if not filtered:
        return None

    # Pick the first (earliest by time.asc)
    return filtered[0]


def _fetch_recent_earnings_news(sym: str, today: date) -> List[str]:
    """
    Fetch a few recent Benzinga news headlines for this ticker,
    biased toward earnings-related titles.
    """
    if not POLYGON_KEY:
        return []

    from_date = (today - timedelta(days=EARNINGS_NEWS_LOOKBACK_DAYS)).isoformat()

    params = {
        "ticker": sym.upper(),
        "date": from_date,
        "limit": 10,
        "sort": "date.desc",
        "order": "desc",
        "apiKey": POLYGON_KEY,
    }

    try:
        r = requests.get(BENZINGA_NEWS_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("results", []) or []
    except Exception as e:
        print(f"[earnings] Benzinga news fetch failed for {sym}: {e}")
        return []

    headlines: List[str] = []
    keywords = ("earnings", "results", "guidance", "quarter", "profit", "revenue")

    for n in items:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        lower = title.lower()
        if any(k in lower for k in keywords):
            headlines.append(title)
        if len(headlines) >= 3:
            break

    # Fallback: if no clearly earnings-related headlines, just take first 2 generic
    if not headlines:
        for n in items[:2]:
            title = (n.get("title") or "").strip()
            if title:
                headlines.append(title)

    return headlines


def _fmt_or_na(v, fmt: str = "{:.2f}") -> str:
    if v is None:
        return "N/A"
    try:
        return fmt.format(v)
    except Exception:
        return "N/A"


# --- Main bot ---

async def run_earnings():
    """
    Earnings Move + Fundamentals Bot:

    Logic:
      1. Use Polygon aggregates to find big 'earnings-style' moves:
         • Price >= MIN_EARNINGS_PRICE
         • |Move vs prior close| >= MIN_EARNINGS_MOVE_PCT
         • Day RVOL >= max(MIN_EARNINGS_RVOL, MIN_RVOL_GLOBAL)
         • Day volume >= MIN_VOLUME_GLOBAL
         • Dollar volume >= MIN_EARNINGS_DOLLAR_VOL
      2. Confirm the ticker actually has an earnings event TODAY via Benzinga Earnings.
      3. Pull:
         • EPS estimates + previous EPS
         • Revenue estimates + previous revenue
         • Surprise % (EPS and/or revenue)
         • Report time (Pre-Market / Intraday / After Close)
         • Recent earnings-related headlines
      4. Send a clean Telegram alert in your preferred format.
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

        # --- Confirm actual earnings event + enrich with fundamentals ---

        earnings_rec = _fetch_earnings_for_symbol(sym, today)
        if not earnings_rec:
            # Big mover, but not actually an earnings event → let other bots handle it
            continue

        # Parse earnings fundamentals
        earn_date = earnings_rec.get("date")
        earn_time = earnings_rec.get("time")
        session_label = _classify_session(earn_date, earn_time)

        est_eps = earnings_rec.get("estimated_eps")
        prev_eps = earnings_rec.get("previous_eps")
        est_rev = earnings_rec.get("estimated_revenue")
        prev_rev = earnings_rec.get("previous_revenue")

        eps_surprise_pct = earnings_rec.get("eps_surprise_percent")
        rev_surprise_pct = earnings_rec.get("revenue_surprise_percent")

        eps_est_str = _fmt_or_na(est_eps)
        eps_prev_str = _fmt_or_na(prev_eps)
        rev_est_str = _fmt_or_na(est_rev, fmt="{:,.0f}")
        rev_prev_str = _fmt_or_na(prev_rev, fmt="{:,.0f}")

        eps_surprise_str = (
            f"EPS {eps_surprise_pct:.1f}%"
            if isinstance(eps_surprise_pct, (int, float))
            else "EPS N/A"
        )
        rev_surprise_str = (
            f"Rev {rev_surprise_pct:.1f}%"
            if isinstance(rev_surprise_pct, (int, float))
            else "Rev N/A"
        )

        # For now we leave IV move as N/A (can wire to options later)
        expected_move_str = "N/A"

        # Recent earnings-related headlines
        headlines = _fetch_recent_earnings_news(sym, today)
        if headlines:
            news_block = "\n".join(f"• {h}" for h in headlines)
        else:
            news_block = "• No obvious earnings headlines in the last few days."

        # Grade + bias using your existing helper
        grade = grade_equity_setup(abs(move_pct), rvol, dollar_vol)
        if move_pct > 0:
            bias = "Long earnings momentum"
        else:
            bias = "Post-earnings fade / short setup"

        # --- Build alert body in your requested style ---

        extra = (
            "✅ Earnings Today\n"
            f"📌 Report Time: {session_label}\n"
            f"💵 EPS: Est {eps_est_str} (Prev {eps_prev_str}) · Rev Est: {rev_est_str}\n"
            f"📊 Last Q Surprise: {eps_surprise_str} · {rev_surprise_str}\n"
            f"📈 Expected Move (IV): {expected_move_str}\n"
            f"📦 Move: {move_pct:.1f}% vs prior close · Gap {gap_pct:.1f}% · Intraday {intraday_pct:.1f}%\n"
            f"🎯 Setup Grade: {grade} · Bias: {bias}\n"
            "📰 Earnings-related headlines:\n"
            f"{news_block}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark_alerted(sym)

        # IMPORTANT:
        # We pass 0.0 for last_price & rvol so the global header is:
        # 📣 EARNINGS — SYM
        # 🕒 timestamp
        # ────────────
        # and your body starts right after.
        send_alert("earnings", sym, 0.0, 0.0, extra=extra)
