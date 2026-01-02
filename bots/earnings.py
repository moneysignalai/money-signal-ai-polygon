import os
import time
from datetime import date, timedelta, datetime, timezone
from typing import List, Optional, Dict, Any, Iterable, Tuple

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    chart_link,
    fetch_benzinga_earnings,
    get_last_trade_cached,
    is_etf_blacklisted,
    now_est,
    send_alert,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG ----------------

MIN_EARNINGS_PRICE = float(os.getenv("MIN_EARNINGS_PRICE", "5"))
MIN_EARNINGS_MOVE_PCT = float(os.getenv("MIN_EARNINGS_MOVE_PCT", "4"))
MIN_EARNINGS_DOLLAR_VOL = float(os.getenv("MIN_EARNINGS_DOLLAR_VOL", "1_000_000"))
EARNINGS_MIN_IMPORTANCE = int(os.getenv("EARNINGS_MIN_IMPORTANCE", "2"))
EARNINGS_ALLOWED_STATUSES = {
    s.strip().lower()
    for s in os.getenv("EARNINGS_ALLOWED_DATE_STATUSES", "confirmed,projected").split(",")
    if s.strip()
}
EARNINGS_EVENT_MAX_AGE_HOURS = float(os.getenv("EARNINGS_EVENT_MAX_AGE_HOURS", "24"))
EARNINGS_POS_SURPRISE_PCT = float(os.getenv("EARNINGS_POS_SURPRISE_PCT", "5"))
EARNINGS_NEG_SURPRISE_PCT = float(os.getenv("EARNINGS_NEG_SURPRISE_PCT", "-5"))

PREMARKET_WINDOW = os.getenv("EARNINGS_PREMARKET_WINDOW", "06:00-10:00")
AFTERHOURS_WINDOW = os.getenv("EARNINGS_AFTERHOURS_WINDOW", "16:00-20:30")
FOLLOWTHROUGH_WINDOW = os.getenv("EARNINGS_FOLLOWTHROUGH_WINDOW", "09:30-11:30")

_CLIENT: Optional[RESTClient] = None

_alert_date: Optional[date] = None
_alerted_event_keys: set[str] = set()


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
    global _alert_date, _alerted_event_keys
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_event_keys = set()


def _already_alerted(key: str) -> bool:
    _reset_if_new_day()
    return key in _alerted_event_keys


def _mark_alerted(key: str) -> None:
    _reset_if_new_day()
    _alerted_event_keys.add(key)


def _parse_window_minutes(raw: str) -> Optional[Tuple[int, int]]:
    try:
        start_s, end_s = raw.split("-")
        start_h, start_m = [int(p) for p in start_s.split(":")]
        end_h, end_m = [int(p) for p in end_s.split(":")]
        return start_h * 60 + start_m, end_h * 60 + end_m
    except Exception:
        return None


def _active_windows() -> List[Tuple[int, int]]:
    windows: List[Tuple[int, int]] = []
    for raw in [PREMARKET_WINDOW, AFTERHOURS_WINDOW, FOLLOWTHROUGH_WINDOW]:
        parsed = _parse_window_minutes(raw)
        if parsed:
            windows.append(parsed)
    return windows


def _within_earnings_windows() -> bool:
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    for start, end in _active_windows():
        if start <= minutes <= end:
            return True
    return False


def _parse_event_time(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _classify_session(event_et: Optional[datetime]) -> str:
    if not event_et:
        return "unknown session"
    hour_min = event_et.hour * 60 + event_et.minute
    if hour_min < 9 * 60 + 30:
        return "premarket"
    if hour_min >= 16 * 60:
        return "afterhours"
    return "during session"


def _event_key(evt: Dict[str, Any]) -> str:
    ticker = str(evt.get("ticker") or evt.get("symbol") or "").upper()
    date_str = str(evt.get("date") or evt.get("earning_date") or "")
    fy = str(evt.get("fiscal_year") or evt.get("fy") or "?")
    fp = str(evt.get("fiscal_period") or evt.get("fp") or "?")
    when = str(evt.get("time") or evt.get("report_time") or "?")
    return f"{ticker}|{date_str}|{fy}|{fp}|{when}"


def _eligible_event(evt: Dict[str, Any], now_utc: datetime) -> Optional[Dict[str, Any]]:
    ticker = str(evt.get("ticker") or evt.get("symbol") or "").upper()
    if not ticker or is_etf_blacklisted(ticker):
        return None

    status = str(evt.get("date_status") or evt.get("status") or "").lower()
    if EARNINGS_ALLOWED_STATUSES and status not in EARNINGS_ALLOWED_STATUSES:
        return None

    importance = evt.get("importance")
    importance_val: Optional[float] = None
    try:
        if importance is not None:
            importance_val = float(importance)
    except Exception:
        importance_val = None
    if importance_val is not None and importance_val < EARNINGS_MIN_IMPORTANCE:
        return None

    event_time_utc = _parse_event_time(str(evt.get("time") or evt.get("report_time")))
    if event_time_utc:
        age_hours = (now_utc - event_time_utc).total_seconds() / 3600.0
        if age_hours > EARNINGS_EVENT_MAX_AGE_HOURS:
            return None

    evt["_event_time_utc"] = event_time_utc
    evt["_event_time_et"] = event_time_utc.astimezone(eastern) if event_time_utc else None
    evt["_session"] = _classify_session(evt.get("_event_time_et"))
    return evt


def _fetch_events_for_dates(dates: Iterable[date]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for d in dates:
        payload = {"date": d.isoformat()}
        data = fetch_benzinga_earnings(payload)
        if not data:
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if results is None and isinstance(data, list):
            results = data
        if not isinstance(results, list):
            continue
        for evt in results:
            if isinstance(evt, dict):
                events.append(evt)
    return events


def _bar_value(bar: Any, attr: str) -> float:
    return float(getattr(bar, attr, None) or getattr(bar, attr[0], 0) or 0) if bar else 0.0


def _aggregate_by_date(bars: List[Any]) -> Dict[date, Any]:
    grouped: Dict[date, Any] = {}
    for bar in bars:
        ts = getattr(bar, "timestamp", None) or getattr(bar, "t", None)
        if ts is None:
            continue
        bar_date = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        grouped[bar_date] = bar
    return grouped


def _price_context(client: RESTClient, ticker: str, today: date) -> Optional[Dict[str, Any]]:
    try:
        bars = list(
            client.list_aggs(
                ticker=ticker,
                multiplier=1,
                timespan="day",
                from_=(today - timedelta(days=40)).isoformat(),
                to=today.isoformat(),
                limit=60,
            )
        )
    except Exception as e:
        print(f"[earnings] daily fetch failed for {ticker}: {e}")
        return None

    if len(bars) < 2:
        return None

    grouped = _aggregate_by_date(bars)
    sorted_dates = sorted(grouped.keys())
    prev_dates = [d for d in sorted_dates if d < today]
    if not prev_dates:
        return None
    prev_date = prev_dates[-1]
    prev_bar = grouped[prev_date]
    prev_close = _bar_value(prev_bar, "close") or _bar_value(prev_bar, "c")
    if prev_close <= 0:
        return None

    today_bar = grouped.get(today)
    last_price, _ = get_last_trade_cached(ticker)
    open_today = _bar_value(today_bar, "open") or _bar_value(today_bar, "o")
    close_today = _bar_value(today_bar, "close") or _bar_value(today_bar, "c")
    volume_today = float(getattr(today_bar, "volume", None) or getattr(today_bar, "v", 0) or 0)

    last_price = last_price or close_today or prev_close

    hist = [grouped[d] for d in prev_dates[-20:]]
    vols = [float(getattr(b, "volume", None) or getattr(b, "v", 0) or 0) for b in hist if b]
    avg_vol = sum(vols) / len(vols) if vols else volume_today
    rvol = (volume_today / avg_vol) if avg_vol else 1.0

    dollar_vol = (last_price or 0) * volume_today if volume_today else 0

    gap_pct = ((open_today - prev_close) / prev_close * 100) if prev_close else 0.0
    move_pct = ((last_price - prev_close) / prev_close * 100) if prev_close else 0.0
    intraday_pct = ((last_price - open_today) / open_today * 100) if open_today else 0.0

    return {
        "prev_close": prev_close,
        "open_today": open_today,
        "last_price": last_price,
        "volume_today": volume_today,
        "rvol": rvol,
        "dollar_vol": dollar_vol,
        "gap_pct": gap_pct,
        "move_pct": move_pct,
        "intraday_pct": intraday_pct,
    }


def _surprise_grade(evt: Dict[str, Any]) -> Tuple[str, str]:
    eps_surprise = evt.get("eps_surprise_percent")
    rev_surprise = evt.get("revenue_surprise_percent") or evt.get("sales_surprise_percent")

    surprises: List[Tuple[str, Optional[float]]] = [
        ("EPS", float(eps_surprise)) if eps_surprise is not None else ("EPS", None),
        ("Revenue", float(rev_surprise)) if rev_surprise is not None else ("Revenue", None),
    ]

    has_data = any(v is not None for _, v in surprises)
    outcome = "none"
    details: List[str] = []
    for label, val in surprises:
        if val is None:
            continue
        details.append(f"{label} surprise: {val:+.1f}%")
        if val >= EARNINGS_POS_SURPRISE_PCT:
            outcome = "beat"
        if val <= EARNINGS_NEG_SURPRISE_PCT:
            if outcome == "beat":
                outcome = "mixed"
            else:
                outcome = "miss"

    if outcome == "none" and has_data:
        outcome = "mixed"

    return outcome, " · ".join(details) if details else "No surprise data"


def _alert_header(outcome: str, price_move: float) -> str:
    if outcome == "beat":
        return "📈 EARNINGS BEAT" if price_move >= 0 else "⚠️ EARNINGS DIVERGENCE"
    if outcome == "miss":
        return "📉 EARNINGS MISS" if price_move <= 0 else "⚠️ EARNINGS DIVERGENCE"
    if outcome == "mixed":
        return "⚠️ EARNINGS DIVERGENCE"
    return "🔥 EARNINGS MOMENTUM"


# ---------------- MAIN BOT ----------------


async def run_earnings():
    BOT_NAME = "earnings"
    if not POLYGON_KEY:
        print("[earnings] POLYGON_KEY missing; skipping.")
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
        return

    client = _get_client()
    if not client:
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
        return

    if not _within_earnings_windows():
        print("[earnings] Outside earnings window; skipping.")
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
        return

    start_ts = time.time()
    now_utc = datetime.now(timezone.utc)
    today = date.today()
    yesterday = today - timedelta(days=1)

    raw_events = _fetch_events_for_dates([today, yesterday])
    scanned = len(raw_events)
    filtered_events: List[Dict[str, Any]] = []

    for evt in raw_events:
        eligible = _eligible_event(evt, now_utc)
        if eligible:
            filtered_events.append(eligible)

    alerts_sent = 0
    matched = 0

    for evt in filtered_events:
        ticker = str(evt.get("ticker") or evt.get("symbol") or "").upper()
        key = _event_key(evt)
        if _already_alerted(key):
            continue

        price_ctx = _price_context(client, ticker, today)
        if not price_ctx:
            continue

        last_price = price_ctx["last_price"]
        if not last_price or last_price < MIN_EARNINGS_PRICE:
            continue

        dollar_vol = price_ctx.get("dollar_vol") or 0
        if dollar_vol < MIN_EARNINGS_DOLLAR_VOL:
            continue

        move_pct = price_ctx.get("move_pct") or 0
        if abs(move_pct) < MIN_EARNINGS_MOVE_PCT:
            continue

        matched += 1

        outcome, surprise_details = _surprise_grade(evt)
        header = _alert_header(outcome, move_pct)
        session = evt.get("_session", "unknown session")
        event_time_et = evt.get("_event_time_et")
        event_time_str = event_time_et.strftime("%I:%M %p ET") if event_time_et else "N/A"

        body_lines = [
            f"{header} — {ticker}",
            f"🕒 {now_est()}",
            f"📆 Event: {evt.get('date')} ({session}, {event_time_str})",
            f"📌 Status: {evt.get('date_status', 'n/a')} · Importance: {evt.get('importance', 'n/a')}",
            f"💰 Price: ${last_price:.2f}",
            f"📊 Move: {move_pct:.1f}% · Gap: {price_ctx['gap_pct']:.1f}% · Intraday: {price_ctx['intraday_pct']:.1f}%",
            f"📦 Vol: {price_ctx['volume_today']:,.0f} (≈ ${dollar_vol:,.0f}) · RVOL: {price_ctx['rvol']:.1f}x",
            f"🧾 Surprise: {surprise_details}",
            f"🔗 Chart: {chart_link(ticker)}",
        ]

        send_alert(BOT_NAME, ticker, last_price, price_ctx["rvol"], extra="\n".join(body_lines))
        _mark_alerted(key)
        alerts_sent += 1

    run_seconds = time.time() - start_ts

    record_bot_stats(
        BOT_NAME,
        scanned=scanned,
        matched=matched,
        alerts=alerts_sent,
        runtime_seconds=run_seconds,
    )

    print(
        f"[earnings] scan complete: scanned={scanned} "
        f"matches={matched} alerts={alerts_sent} "
        f"runtime={run_seconds:.2f}s"
    )
