# bots/shared.py
import asyncio
import os
import time
import math
import json
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

import pytz
import requests

# ---------------- BASIC CONFIG ----------------

POLYGON_KEY = os.getenv("POLYGON_KEY") or os.getenv("POLYGON_API_KEY")
API_BASE = os.getenv("POLYGON_BASE_URL", "https://api.polygon.io")

# Global RVOL / volume floors that other bots can reference
MIN_RVOL_GLOBAL = float(os.getenv("MIN_RVOL_GLOBAL", "2.0"))
MIN_VOLUME_GLOBAL = float(os.getenv("MIN_VOLUME_GLOBAL", "500000"))  # shares

# Telegram routing (your env)
# - TELEGRAM_CHAT_ALL      = single private chat ID for everything
# - TELEGRAM_TOKEN_ALERTS  = all-in-one alerts bot for all trade signals
# - TELEGRAM_TOKEN_STATUS  = dedicated status/heartbeat bot
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

# Some bots may want a separate status-only chat; if not set, fallback to CHAT_ALL
TELEGRAM_CHAT_STATUS = os.getenv("TELEGRAM_CHAT_STATUS") or TELEGRAM_CHAT_ALL

# ----------------------------------------------------------------------
# Bot-level config flags (for system-level control & debugging)
# ----------------------------------------------------------------------

# DISABLED_BOTS: comma-separated list of bot names that should NOT run at all.
# Example: DISABLED_BOTS=daily_ideas,options_indicator
DISABLED_BOTS = {
    b.strip().lower()
    for b in os.getenv("DISABLED_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# TEST_MODE_BOTS: comma-separated list of bot names that should run in "shadow" / test mode.
# Example: TEST_MODE_BOTS=options_flow,rsi_signals
TEST_MODE_BOTS = {
    b.strip().lower()
    for b in os.getenv("TEST_MODE_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# DEBUG_FLOW_REASONS: if true, bots can log why candidates were rejected by filters.
# Example: DEBUG_FLOW_REASONS=true
DEBUG_FLOW_REASONS = os.getenv("DEBUG_FLOW_REASONS", "false").lower() == "true"

eastern = pytz.timezone("US/Eastern")


# ---------------- TIME HELPERS ----------------


def now_est() -> str:
    """
    Human-friendly time string in Eastern, e.g. '10:48 AM EST · Nov 20'.

    NOTE: this returns a STRING on purpose, so bots can just do:
        ts = now_est()
    and drop it straight into messages.
    """
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


def now_est_dt() -> datetime:
    """Timezone-aware Eastern datetime for consistent trading-day math."""

    return datetime.now(eastern)


def format_est_timestamp(ts: Optional[datetime] = None) -> str:
    """Return an Eastern timestamp in MM-DD-YYYY · HH:MM AM/PM EST format."""

    if ts:
        dt = ts if ts.tzinfo else eastern.localize(ts)
        dt = dt.astimezone(eastern)
    else:
        dt = datetime.now(eastern)
    return dt.strftime("%m-%d-%Y · %I:%M %p EST")


def today_est_date() -> date:
    return datetime.now(eastern).date()


def is_trading_day_est() -> bool:
    """Return True on US/Eastern weekdays (Mon–Fri)."""
    return today_est_date().weekday() < 5


def iso_today() -> str:
    return today_est_date().isoformat()


def minutes_since_midnight_est() -> int:
    now = datetime.now(eastern)
    return now.hour * 60 + now.minute


# ----------------------------------------------------------------------
# Pretty-format helpers (for contracts / debug)
# ----------------------------------------------------------------------


def pretty_contract(raw: str) -> str:
    """
    Convert Polygon-style contract symbols like:
        O:TSLA251121C00450000
    into human-readable:
        TSLA 11/21/25 450C

    If parsing fails, returns the original string.
    """
    try:
        if not raw or not raw.startswith("O:"):
            return raw

        core = raw[2:]  # TSLA251121C00450000
        if len(core) < 15:
            return raw

        underlying = core[:-15]
        date_part = core[-15:-9]  # YYMMDD
        cp = core[-9:-8]          # C / P
        strike_part = core[-8:]   # 00450000 -> 450.000

        yy = int(date_part[0:2]) + 2000
        mm = int(date_part[2:4])
        dd = int(date_part[4:6])

        strike_int = int(strike_part)
        strike = strike_int / 1000.0

        exp_fmt = f"{mm:02d}/{dd:02d}/{str(yy)[2:]}"  # 11/21/25
        cp_letter = cp.upper() if cp in ("C", "P") else "?"

        return f"{underlying} {exp_fmt} {strike:g}{cp_letter}"
    except Exception:
        return raw


# ----------------------------------------------------------------------
# Bot-mode helper functions
# ----------------------------------------------------------------------


def is_bot_disabled(bot_name: str) -> bool:
    """
    Return True if this bot is globally disabled via env (DISABLED_BOTS).

    This is primarily used by scheduler / bots to early-exit.
    """
    return bot_name.strip().lower() in DISABLED_BOTS


def is_bot_test_mode(bot_name: str) -> bool:
    """
    Return True if this bot is in TEST_MODE_BOTS.

    Bots can use this to:
      • avoid sending real alerts (shadow mode), or
      • route alerts to a separate Telegram chat, etc.
    """
    return bot_name.strip().lower() in TEST_MODE_BOTS


def debug_filter_reason(bot_name: str, symbol: str, reason: str) -> None:
    """
    Optional debugging helper.

    If DEBUG_FLOW_REASONS=true, bots can call this to log why a candidate
    was rejected by filters. This is useful when you see scanned>0 but alerts=0
    and want to understand which filter is doing the blocking.

    Output is cleaned up:
      • Polygon option tickers (O:TSLA251121C00450000) are converted to:
            TSLA 11/21/25 450C
      • One-line, emoji-tagged, timestamped.
    """
    if not DEBUG_FLOW_REASONS:
        return

    # Clean up any Polygon-style option symbols in the reason text
    parts = reason.split()
    cleaned_parts: List[str] = []
    for p in parts:
        if p.startswith("O:"):
            cleaned_parts.append(pretty_contract(p))
        else:
            cleaned_parts.append(p)
    cleaned_reason = " ".join(cleaned_parts)

    ts = now_est()
    # Example:
    # 🐞 DEBUG — options_flow | TSLA | 10:15 AM EST · Nov 30 → no last trade for TSLA 11/21/25 450C
    print(f"🐞 DEBUG — {bot_name} | {symbol} | {ts} → {cleaned_reason}")


# ---------------- TELEGRAM CORE ----------------


def _send_telegram_raw(token: str, chat_id: str, text: str, parse_mode: Optional[str] = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        # We deliberately do not raise; status bot might still be able to report.
        print(f"[telegram] failed to send: {e} | text={text!r}")


def send_alert(
    bot_name: str,
    symbol: str,
    last_price: float,
    rvol: float,
    extra: Optional[str] = None,
) -> None:
    """
    Core alert sender used by all bots.
    Sends a pretty Telegram message via TELEGRAM_TOKEN_ALERTS → TELEGRAM_CHAT_ALL.
    """
    token = TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[alert:{bot_name}] (no TELEGRAM_TOKEN_ALERTS or TELEGRAM_CHAT_ALL) {symbol} {extra}")
        return

    header = f"🧠 {bot_name.upper()} — {symbol}"
    body_lines = [header]
    if last_price:
        body_lines.append(f"💰 Last: ${last_price:.2f}")
    if rvol:
        body_lines.append(f"📊 RVOL: {rvol:.1f}x")
    if extra:
        body_lines.append("────────────")
        body_lines.append(extra)

    text = "\n".join(body_lines)
    _send_telegram_raw(token, chat, text, parse_mode=None)


def send_alert_text(text: str) -> None:
    """Send a preformatted alert message.

    This is useful for bots (e.g., the option flow family) that construct a
    fully formatted multi-line string and simply need it delivered to the
    alerts channel without additional headers or embellishments.
    """

    token = TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[alert:custom] (no TELEGRAM_TOKEN_ALERTS or TELEGRAM_CHAT_ALL) {text}")
        return
    _send_telegram_raw(token, chat, text, parse_mode=None)


# ---------------- STATUS / ERROR REPORTING ----------------


def _send_status(text: str) -> None:
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS
    chat = TELEGRAM_CHAT_STATUS or TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[status] (no TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL) {text}")
        return
    _send_telegram_raw(token, chat, text, parse_mode=None)


def report_status_error(bot: str, message: str) -> None:
    """
    Soft error reporting to the status bot.

    Called by shared helpers when a non-fatal error happens (e.g. Polygon fails),
    so it shows up in Telegram even if the main bot continues.
    """
    ts = now_est()
    text = f"⚠️ [{bot}] {ts}\n{message}"
    _send_status(text)


# ---------------- BOT STATS (for status_report.py) ----------------

STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")


def _load_stats_file() -> Dict[str, Any]:
    """Internal helper: load the JSON stats file, or return empty."""
    try:
        if os.path.exists(STATS_PATH):
            with open(STATS_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        print(f"[record_bot_stats] failed to read stats file: {e}")
    return {}


def _save_stats_file(data: Dict[str, Any]) -> None:
    """Internal helper: save the JSON stats file atomically, swallowing errors."""
    try:
        os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    except Exception:
        pass

    try:
        tmp_path = f"{STATS_PATH}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, STATS_PATH)
    except Exception as e:
        msg = f"[record_bot_stats] failed to write stats file: {e}"
        print(msg)
        # soft report so it shows in status if possible
        try:
            report_status_error("status_report", msg)
        except Exception:
            pass


def record_bot_stats(
    bot_name: str,
    scanned: int,
    matched: int,
    alerts: int,
    runtime_seconds: Optional[float] = None,
    *,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> None:
    """Record per-bot stats with trading-day scoping.

    The signature remains backward compatible (runtime_seconds positional) so
    existing bots keep working, but callers are encouraged to pass explicit
    ``started_at``/``finished_at`` datetimes for better accuracy.
    """

    bot_name = str(bot_name)

    finished = finished_at or now_est_dt()
    # Infer start time if missing
    if started_at is None and runtime_seconds is not None:
        started_at = finished - timedelta(seconds=float(runtime_seconds))
    started = started_at or finished
    runtime = runtime_seconds
    if runtime is None:
        runtime = max((finished - started).total_seconds(), 0.0)

    trading_day = finished.astimezone(eastern).date().isoformat()

    entry = {
        "bot_name": bot_name,
        "scanned": int(scanned),
        "matched": int(matched),
        "alerts": int(alerts),
        "runtime": float(runtime),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "finished_at_ts": finished.timestamp(),
        "finished_at_str": format_est_timestamp(finished),
        "trading_day": trading_day,
    }

    data = _load_stats_file()
    bots = data.setdefault("bots", {})

    prev = bots.get(bot_name, {}) or {}
    history: List[Dict[str, Any]] = []
    if isinstance(prev, dict):
        hist_obj = prev.get("history")
        if isinstance(hist_obj, list):
            for item in hist_obj:
                if isinstance(item, dict):
                    history.append(item)
        # If the legacy structure only had a flat latest record, preserve it
        if not history and {"scanned", "matched", "alerts"}.issubset(prev.keys()):
            history.append(prev)

    history.append(entry)
    if len(history) > 100:
        history = history[-100:]

    bots[bot_name] = {"latest": entry, "history": history}
    data["bots"] = bots

    _save_stats_file(data)


# ---------------- HTTP HELPER WITH RETRIES ----------------


def _http_get_json(
    url: str,
    params: Dict[str, Any],
    *,
    tag: str,
    timeout: float = 20.0,
    retries: int = 2,
    backoff_seconds: float = 2.5,
) -> Optional[Dict[str, Any]]:
    """
    Thin wrapper around requests.get with:
      • configurable timeout
      • a few retries with exponential backoff
      • optional status reporting on final failure

    This is used for Polygon grouped/agg/snapshot GETs so that transient
    slowness does not kill the bots or flood them with exceptions.
    """
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            # Graceful handling of rate limits
            if resp.status_code == 429:
                wait = backoff_seconds * (attempt + 1)
                print(f"[{tag}] Polygon 429 rate-limit; sleeping {wait:.1f}s before retry.")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < retries:
                wait = backoff_seconds * (attempt + 1)
                print(f"[{tag}] HTTP error on attempt {attempt+1}/{retries+1}: {e} — retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                msg = f"[{tag}] error after {retries+1} attempts: {e}"
                print(msg)
                report_status_error(tag, msg)
                return None
    return None


# ---------------- DYNAMIC / CONFIGURABLE UNIVERSE ----------------

_UNIVERSE_CACHE: Dict[str, Any] = {"ts": 0.0, "data": []}
_EMERGENCY_UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "TSLA"]


def _parse_ticker_env(raw: str) -> List[str]:
    """
    Parse a comma-separated env string into a de-duplicated, uppercased ticker list.
    """
    if not raw:
        return []
    tickers: List[str] = []
    for part in raw.replace(" ", "").replace('"', "").split(","):
        if not part:
            continue
        sym = part.upper()
        if sym not in tickers:
            tickers.append(sym)
    return tickers


def _get_env_universe(env_var: str) -> List[str]:
    """
    Helper: read a specific env var (e.g. TICKER_UNIVERSE) and return its tickers.
    """
    raw = os.getenv(env_var, "")
    if not raw.strip():
        return []
    tickers = _parse_ticker_env(raw)
    if not tickers:
        return []
    print(f"[shared] using {env_var} universe: {len(tickers)} names")
    return tickers


def _get_options_override_universe() -> List[str]:
    """Return an explicit options override universe if provided via env."""

    override = os.getenv("OPTIONS_FLOW_TICKER_UNIVERSE", "")
    if not override.strip():
        return []
    tickers = _parse_ticker_env(override)
    if tickers:
        print(
            f"[universe] using OPTIONS_FLOW_TICKER_UNIVERSE override size={len(tickers)}"
        )
    return tickers


MAX_UNIVERSE_CAP = int(os.getenv("UNIVERSE_TOP_N", "250") or 250)


def _should_log_universe(now_ts: float) -> bool:
    """Emit a universe log at most once per minute to avoid spam."""

    last_log = _UNIVERSE_CACHE.get("log_ts") or 0.0
    if now_ts - float(last_log) >= 60.0:
        _UNIVERSE_CACHE["log_ts"] = now_ts
        return True
    return False


def _get_top_volume_universe_sync(
    max_tickers: int = MAX_UNIVERSE_CAP, volume_coverage: Optional[float] = None
) -> List[str]:
    """Return a liquid universe ordered by dollar volume with layered fallbacks."""

    now_ts = time.time()
    if _UNIVERSE_CACHE["data"] and now_ts - float(_UNIVERSE_CACHE["ts"]) < 60.0:
        cached = _UNIVERSE_CACHE["data"][:max_tickers]
        if _should_log_universe(now_ts):
            print(
                f"[universe] using cached top-volume universe size={len(cached)} "
                f"(source=TOP_250_VOLUME)"
            )
        return cached

    try:
        env_cap = int(os.getenv("DYNAMIC_MAX_TICKERS", str(max_tickers)))
        max_tickers = max(1, min(max_tickers, env_cap, MAX_UNIVERSE_CAP))
    except Exception:
        max_tickers = max(1, min(max_tickers, MAX_UNIVERSE_CAP))

    tickers: List[Tuple[str, float]] = []
    grouped_source = None
    if POLYGON_KEY:
        # Try to find the most recent trading day with grouped results to avoid
        # weekend/holiday empty universes. Look back up to one week.
        today = today_est_date()
        for offset in range(1, 8):
            day = today - timedelta(days=offset)
            from_ = day.isoformat()
            url = f"{API_BASE}/v2/aggs/grouped/locale/us/market/stocks/{from_}"
            params = {"adjusted": "true", "apiKey": POLYGON_KEY}
            data = _http_get_json(
                url,
                params,
                tag="shared:universe",
                timeout=7.0,
                retries=1,
                backoff_seconds=2.0,
            )
            results = data.get("results") if data else None
            if results:
                grouped_source = from_
                for row in results:
                    sym = row.get("T")
                    vol = float(row.get("v") or 0.0)
                    vwap = float(row.get("vw") or 0.0)
                    dollar_vol = vol * max(vwap, 0.0)
                    if not sym or dollar_vol <= 0:
                        continue
                    tickers.append((sym, dollar_vol))
                break
        if grouped_source and _should_log_universe(now_ts):
            print(
                f"[universe] using grouped date={grouped_source} source=TOP_{MAX_UNIVERSE_CAP}_VOLUME"
            )
    if tickers:
        tickers.sort(key=lambda x: x[1], reverse=True)
        total_dollar = sum(row[1] for row in tickers)
        universe: List[str] = []
        running = 0.0
        for sym, dv in tickers:
            universe.append(sym)
            running += dv
            if len(universe) >= max_tickers:
                break
            if volume_coverage and total_dollar > 0 and running / total_dollar >= volume_coverage:
                break
        _UNIVERSE_CACHE["ts"] = now_ts
        _UNIVERSE_CACHE["data"] = universe
        _UNIVERSE_CACHE["log_ts"] = now_ts
        print(
            f"[universe] using top-volume universe size={len(universe)} "
            f"(source=TOP_{MAX_UNIVERSE_CAP}_VOLUME)"
        )
        return universe[:max_tickers]

    env_universe = _get_env_universe("TICKER_UNIVERSE")
    if not env_universe:
        env_universe = _get_env_universe("FALLBACK_TICKER_UNIVERSE")

    if env_universe:
        _UNIVERSE_CACHE["ts"] = now_ts
        _UNIVERSE_CACHE["data"] = env_universe
        _UNIVERSE_CACHE["log_ts"] = now_ts
        print(
            f"[universe] massive volume feed unavailable, using ENV TICKER_UNIVERSE "
            f"size={len(env_universe)}"
        )
        return env_universe[:max_tickers]

    print("[universe] CRITICAL: universe empty — using emergency minimal fallback set")
    _UNIVERSE_CACHE["ts"] = now_ts
    _UNIVERSE_CACHE["data"] = _EMERGENCY_UNIVERSE
    _UNIVERSE_CACHE["log_ts"] = now_ts
    return _EMERGENCY_UNIVERSE[:max_tickers]


async def get_top_volume_universe(
    limit: int = MAX_UNIVERSE_CAP, volume_coverage: Optional[float] = None
) -> List[str]:
    """Async helper wrapper for fetching the top-volume universe with fallbacks."""

    return await asyncio.to_thread(
        _get_top_volume_universe_sync, limit, volume_coverage
    )


def get_dynamic_top_volume_universe(
    max_tickers: int = MAX_UNIVERSE_CAP, volume_coverage: Optional[float] = None
) -> List[str]:
    """Backwards-compatible wrapper for older callers (uses top-volume resolver)."""

    return _get_top_volume_universe_sync(max_tickers, volume_coverage)


async def resolve_options_underlying_universe(
    bot_name: str,
    *,
    max_tickers: Optional[int] = None,
    allow_top_volume_fallback: bool = True,
) -> List[str]:
    """Resolve an options underlying universe with layered fallbacks.

    Priority:
    1) OPTIONS_FLOW_TICKER_UNIVERSE override (if set)
    2) Top-volume universe from Massive/Polygon (cached)
    3) ENV TICKER_UNIVERSE (or FALLBACK_TICKER_UNIVERSE)
    4) Emergency minimal list to avoid empty scans
    """

    try:
        env_cap = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", str(MAX_UNIVERSE_CAP)))
    except Exception:
        env_cap = MAX_UNIVERSE_CAP
    env_cap = min(env_cap, MAX_UNIVERSE_CAP)
    if max_tickers is None:
        max_tickers = env_cap
    else:
        max_tickers = min(max_tickers, env_cap)

    override = _get_options_override_universe()
    if override:
        final_override = override[:max_tickers]
        print(
            f"[universe:{bot_name}] options_underlying_universe_size={len(final_override)} (source=override)"
        )
        return final_override

    if allow_top_volume_fallback:
        universe = await get_top_volume_universe(limit=max_tickers)
        if universe:
            return universe[:max_tickers]

    env_universe = _get_env_universe("TICKER_UNIVERSE")
    if not env_universe:
        env_universe = _get_env_universe("FALLBACK_TICKER_UNIVERSE")
    if env_universe:
        trimmed = env_universe[:max_tickers]
        print(
            f"[universe:{bot_name}] massive volume feed unavailable, using ENV TICKER_UNIVERSE size={len(trimmed)}"
        )
        return trimmed

    print("[universe] CRITICAL: universe empty — using emergency minimal fallback set")
    return _EMERGENCY_UNIVERSE[:max_tickers]


def resolve_universe_for_bot(
    bot_name: str,
    bot_env_var: Optional[str] = None,
    base_env_universe: str = "TICKER_UNIVERSE",
    max_universe_env: Optional[str] = None,
    default_max_universe: Optional[int] = None,
    apply_dynamic_filters: bool = True,
    volume_coverage_env: str = "DYNAMIC_VOLUME_COVERAGE",
) -> List[str]:
    """
    Unified universe resolver for all bots.

    Priority:
      1) If `bot_env_var` (e.g. OPTIONS_FLOW_TICKER_UNIVERSE) is set → use that.
      2) Else if `base_env_universe` (default: TICKER_UNIVERSE) is set → use that.
      3) Else → fall back to dynamic Polygon/Massive universe or FALLBACK_TICKER_UNIVERSE.

    Configuration knobs:
      • max_universe_env (e.g. EQUITY_FLOW_MAX_UNIVERSE) caps the list if set.
      • default_max_universe falls back if no max env is set (defaults to
        DYNAMIC_MAX_TICKERS when omitted).
      • apply_dynamic_filters trims the chosen universe to the most liquid names
        using get_dynamic_top_volume_universe. This helps keep counts consistent
        across bots even when TICKER_UNIVERSE is large.
    """

    def _int_env(name: str) -> Optional[int]:
        try:
            return int(os.getenv(name, "")) if os.getenv(name, "").strip() else None
        except Exception:
            return None

    dyn_cap = _int_env("DYNAMIC_MAX_TICKERS") or default_max_universe or MAX_UNIVERSE_CAP
    # Hard cap to 250 per requirements to avoid overly wide scans.
    dyn_cap = min(dyn_cap, MAX_UNIVERSE_CAP)
    resolved_max = dyn_cap
    if max_universe_env:
        env_cap = _int_env(max_universe_env)
        if env_cap:
            resolved_max = min(resolved_max, env_cap)

    coverage_val: Optional[float] = None
    if volume_coverage_env:
        try:
            env_cov = os.getenv(volume_coverage_env)
            if env_cov:
                coverage_val = float(env_cov)
        except Exception:
            coverage_val = None

    # 1) Per-bot override (e.g. OPTIONS_FLOW_TICKER_UNIVERSE)
    selected_universe: List[str] = []
    if bot_env_var:
        override = _get_env_universe(bot_env_var)
        if override:
            selected_universe = override

    # 2) Base env
    if not selected_universe:
        base_env = _get_env_universe(base_env_universe)
        if base_env:
            selected_universe = base_env

    # 3) Dynamic fallback when no env universe is present
    if not selected_universe:
        universe = get_dynamic_top_volume_universe(
            max_tickers=resolved_max,
            volume_coverage=coverage_val,
        )
        print(f"[shared] {bot_name}: using dynamic universe ({len(universe)} names)")
        return universe

    trimmed = selected_universe
    if apply_dynamic_filters:
        liquid = get_dynamic_top_volume_universe(
            max_tickers=resolved_max,
            volume_coverage=coverage_val,
        )
        if liquid:
            liquid_set = set(liquid)
            trimmed = [t for t in selected_universe if t in liquid_set]
        else:
            trimmed = selected_universe

    if len(trimmed) > resolved_max:
        print(
            f"[shared] {bot_name}: capping universe from {len(trimmed)} → {resolved_max}"
        )
    final_universe = trimmed[:resolved_max]
    print(
        f"[shared] {bot_name}: universe {len(final_universe)} names "
        f"(max={resolved_max}, dynamic={'on' if apply_dynamic_filters else 'off'})"
    )
    return final_universe


# ---------------- ETF BLACKLIST ----------------

ETF_BLACKLIST = {
    "DIA",
    "VTI",
    "XLK",
    "XLF",
    "XLE",
    "XLY",
    "XLI",
    "XLV",
    "XLP",
    "XLB",
    "XLU",
    "XOP",
    "XRT",
}


def is_etf_blacklisted(symbol: str) -> bool:
    return symbol.upper() in ETF_BLACKLIST


# ---------------- CACHED UNDERLYING LAST ----------------

@dataclass
class LastTradeCacheEntry:
    ts: float
    last: float
    dollar_vol: float


_LAST_TRADE_CACHE: Dict[str, LastTradeCacheEntry] = {}


def get_last_trade_cached(symbol: str, ttl_seconds: int = 15) -> Tuple[Optional[float], Optional[float]]:
    """
    Cached last / dollar volume for the underlying (equity/ETF).
    Uses v2 last trade + MIN_VOLUME_GLOBAL as a rough dollar-vol approximation.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last trade.")
        return None, None

    key = symbol.upper()
    now_ts = time.time()
    entry = _LAST_TRADE_CACHE.get(key)
    if entry and isinstance(entry.ts, (int, float)) and now_ts - float(entry.ts) < ttl_seconds:
        return entry.last, entry.dollar_vol

    url = f"{API_BASE}/v2/last/trade/{symbol.upper()}"
    params = {"apiKey": POLYGON_KEY}

    data = _http_get_json(url, params, tag="shared:last_trade", timeout=15.0, retries=1)
    if not data:
        return None, None

    results = data.get("results")
    if isinstance(results, dict):
        last_raw = results.get("p")
    else:
        last_raw = None

    try:
        last_price = float(last_raw) if last_raw is not None else None
    except (TypeError, ValueError):
        last_price = None

    if last_price is None or last_price <= 0:
        return None, None

    dollar_vol = last_price * MIN_VOLUME_GLOBAL
    _LAST_TRADE_CACHE[key] = LastTradeCacheEntry(ts=now_ts, last=last_price, dollar_vol=dollar_vol)
    return last_price, dollar_vol


# ---------------- OPTION CACHES ----------------

@dataclass
class OptionCacheEntry:
    ts: float
    data: Dict[str, Any]


_OPTION_CACHE: Dict[str, OptionCacheEntry] = {}


def _cache_key(prefix: str, identifier: str) -> str:
    return f"{prefix}:{identifier}"


def get_option_chain_cached(
    underlying: str,
    ttl_seconds: int = 90,
) -> Optional[Dict[str, Any]]:
    """Fetches Polygon snapshot option chain via HTTP and caches it.

    Used by options_flow and any options-related logic.
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch option chain.")
        return None

    key = _cache_key("chain", underlying.upper())
    now_ts = time.time()

    entry = _OPTION_CACHE.get(key)
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now_ts - float(entry.ts) < ttl_seconds:
            return entry.data

    url = f"{API_BASE}/v3/snapshot/options/{underlying.upper()}"
    params = {"apiKey": POLYGON_KEY}

    data = _http_get_json(url, params, tag="shared:option_chain", timeout=20.0, retries=1)
    if not data:
        return None

    _OPTION_CACHE[key] = OptionCacheEntry(ts=now_ts, data=data)
    return data


def get_last_option_trades_cached(
    full_option_symbol: str,
    ttl_seconds: int = 45,
) -> Optional[Dict[str, Any]]:
    """
    Fetches the last option trade for a specific contract (v2 last/trade/{optionsTicker}).

    Improvements:
      • Slightly longer timeout (default ~20s).
      • One retry with backoff.
      • 404 is treated as benign (no status spam).
    """
    if not POLYGON_KEY:
        print("[shared] POLYGON_KEY missing; cannot fetch last option trades.")
        return None

    key = _cache_key("last_trade", full_option_symbol)
    now_ts = time.time()

    entry = _OPTION_CACHE.get(key)
    if isinstance(entry, OptionCacheEntry) and isinstance(entry.ts, (int, float)):
        if now_ts - float(entry.ts) < ttl_seconds:
            return entry.data

    # Polygon-compatible last-trade endpoint for options:
    #    /v2/last/trade/{optionsTicker}
    url = f"{API_BASE}/v2/last/trade/{full_option_symbol}"
    params = {"apiKey": POLYGON_KEY}

    timeout = 20.0
    retries = 1
    backoff_seconds = 2.5

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 404:
                # Benign: no last option trade exists yet for this contract.
                return None
            resp.raise_for_status()
            data = resp.json()
            _OPTION_CACHE[key] = OptionCacheEntry(ts=now_ts, data=data)
            return data
        except Exception as e:
            if attempt < retries:
                wait = backoff_seconds * (attempt + 1)
                print(
                    f"[shared:last_option_trade] HTTP error on attempt "
                    f"{attempt+1}/{retries+1}: {e} — retrying in {wait:.1f}s"
                )
                time.sleep(wait)
            else:
                msg = (
                    f"[shared] error fetching last option trade for "
                    f"{full_option_symbol}: {e}"
                )
                print(msg)
                report_status_error("shared:last_option_trade", msg)
                return None

    return None


# Legacy camelCase aliases
def getOptionChainCached(underlying: str, ttl_seconds: int = 60):
    return get_option_chain_cached(underlying, ttl_seconds=ttl_seconds)


def getLastOptionTradesCached(full_option_symbol: str, ttl_seconds: int = 30):
    return get_last_option_trades_cached(full_option_symbol, ttl_seconds=ttl_seconds)


# ---------------- CHART LINK ----------------


def chart_link(symbol: str, timeframe: str = "D", provider: Optional[str] = None) -> str:
    """Return a TradingView chart link.

    The optional ``timeframe`` and ``provider`` parameters are accepted for
    backwards compatibility with existing bot calls; they do not change the
    returned URL but allow callers to pass named arguments without raising.
    """

    _ = timeframe  # kept for signature compatibility / clarity
    _ = provider
    return f"https://www.tradingview.com/chart/?symbol={symbol.upper()}"


# ---------------- GRADING ----------------


def grade_equity_setup(
    move_pct: float,
    rvol: float,
    dollar_vol: float,
) -> str:
    """Simple letter grade: A+ / A / B / C based on strength."""
    score = 0.0

    score += max(0.0, min(rvol / 2.0, 3.0))  # up to 3 points
    score += max(0.0, min(abs(move_pct) / 3.0, 3.0))  # up to 3
    score += max(0.0, min(math.log10(max(dollar_vol, 1.0)) - 6.0, 2.0))  # up to 2

    if score >= 7.0:
        return "A+"
    if score >= 5.5:
        return "A"
    if score >= 4.0:
        return "B"
    return "C"


# ---------------- TIME WINDOWS HELPERS ----------------


def is_between_times(
    start_h: int,
    start_m: int,
    end_h: int,
    end_m: int,
    tz: pytz.timezone,
) -> bool:
    now = datetime.now(tz)
    mins = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    return start <= mins <= end


def is_rth() -> bool:
    """Regular trading hours 09:30–16:00 ET."""
    return is_between_times(9, 30, 16, 0, eastern)


def is_premarket() -> bool:
    return is_between_times(4, 0, 9, 29, eastern)


def in_premarket_window_est() -> bool:
    """Explicit helper for 04:00–09:30 ET premarket window."""
    return is_premarket()


def is_postmarket() -> bool:
    return is_between_times(16, 1, 20, 0, eastern)


def in_rth_window_est(start_minute: int = 0, end_minute: int = 390) -> bool:
    """Return True if now is within a sub-window of regular trading hours (ET).

    start_minute/end_minute are offsets in minutes from the 09:30 ET open.

    Examples:
      • in_rth_window_est() → full RTH (09:30–16:00).
      • in_rth_window_est(0, 60) → first hour after open.
      • in_rth_window_est(60, 240) → between 10:30–13:30 ET.
    """
    if not is_rth():
        return False

    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    rth_start = 9 * 60 + 30
    offset = mins - rth_start
    return start_minute <= offset <= end_minute