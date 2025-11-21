# bots/status_report.py
#
# Central status / heartbeat + error digest bot.
#
# Responsibilities:
#   • Collect errors from all bots via record_bot_error(...)
#   • Optionally collect shared/universe info via record_shared_error(...) and record_universe_health(...)
#   • On each run_status_report() call:
#       - If there are NEW errors since last digest → send an error summary to the status Telegram.
#       - Else, send a periodic heartbeat (startup + every ~10 minutes).

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import pytz
import requests
import threading

# --------------- CONFIG / ENV ---------------

eastern = pytz.timezone("US/Eastern")

TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")

# --------------- BOT RUN LOGGING ---------------

# (timestamp, bot_name, outcome) where outcome in {"ok", "error"}
_BOT_RUN_LOG: List[Tuple[datetime, str, str]] = []


def log_bot_run(bot_name: str, outcome: str) -> None:
    """
    Called by main.py after each bot finishes.

    outcome:
      • "ok"    → bot finished without raising an exception
      • "error" → bot raised an exception
    """
    now_et = datetime.now(eastern)
    _BOT_RUN_LOG.append((now_et, bot_name, outcome))

    # keep the buffer from growing unbounded
    if len(_BOT_RUN_LOG) > 500:
        del _BOT_RUN_LOG[:-500]


def consume_recent_bot_runs() -> List[Tuple[datetime, str, str]]:
    """
    Pop and return all accumulated bot run entries since the last status cycle.
    """
    global _BOT_RUN_LOG
    events = _BOT_RUN_LOG
    _BOT_RUN_LOG = []
    return events


# --------------- INTERNAL STATE ---------------

# Error events: list of dicts {time, source, message}
_error_events: List[Dict] = []

# Last time we sent an error digest (to avoid spamming the same ones)
_last_error_digest_sent_at: Optional[datetime] = None

# Latest dynamic-universe health snapshot
_universe_health: Optional[Dict] = None  # {"time": dt, "size": int, "coverage": float}

# Last heartbeat we sent (no-error status message)
_last_heartbeat_sent_at: Optional[datetime] = None

# Whether we've sent at least one heartbeat since startup
_startup_heartbeat_sent: bool = False

# Simple lock so multiple bots recording at once don't corrupt state
_LOCK = threading.Lock()


# --------------- TIME HELPERS ---------------

def now_est() -> datetime:
    """Current time in US/Eastern."""
    return datetime.now(eastern)


def _format_est_timestamp(dt: datetime) -> str:
    return dt.strftime("%I:%M %p EST · %b %d").lstrip("0")


# --------------- TELEGRAM LOW-LEVEL ---------------

def _send_status_telegram(text: str) -> None:
    """
    Send a status / heartbeat message to the dedicated status bot.

    Uses:
      • TELEGRAM_TOKEN_STATUS
      • TELEGRAM_CHAT_ALL
    """
    token = TELEGRAM_TOKEN_STATUS
    chat_id = TELEGRAM_CHAT_ALL

    if not token or not chat_id:
        # Don't crash the app just because env isn't wired.
        print(
            "[status_report] missing TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL; "
            "status message not sent. Text was:\n"
            f"{text}"
        )
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                # NO parse_mode to avoid 'can't parse entities' 400 errors.
            },
            timeout=8,
        )
        if resp.status_code != 200:
            print(f"[status_report] telegram send failed {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"[status_report] telegram send exception: {e}")


# --------------- PUBLIC RECORDING API ---------------

def record_bot_error(source: str, exc: Exception | str) -> None:
    """
    Called by main.py when a bot raises, e.g.:

        record_bot_error("unusual", e)

    or by other places that want to centralize error reporting.
    """
    msg = str(exc)
    event = {
        "time": now_est(),
        "source": source,
        "message": msg,
    }
    with _LOCK:
        _error_events.append(event)
        # Keep buffer reasonably small
        if len(_error_events) > 300:
            _error_events[:] = _error_events[-200:]


def record_shared_error(tag: str, message: str) -> None:
    """
    Convenience wrapper for shared.py, so you can log:

        record_shared_error("universe", "[shared] error fetching dynamic universe: ...")

    It will show up as source = "shared:universe".
    """
    source = f"shared:{tag}"
    record_bot_error(source, message)


def record_universe_health(size: int, coverage: float) -> None:
    """
    Called by shared.get_dynamic_top_volume_universe(...) on successful universe builds.

    Example usage in shared.py:

        from bots.status_report import record_universe_health
        ...
        record_universe_health(len(tickers), coverage)

    This lets the status bot include universe stats in periodic heartbeats.
    """
    snap = {
        "time": now_est(),
        "size": int(size),
        "coverage": float(coverage),
    }
    with _LOCK:
        global _universe_health
        _universe_health = snap


# --------------- FORMAT HELPERS ---------------

def _build_error_digest(now: datetime) -> Optional[str]:
    """
    Build a human-readable digest of NEW errors since last digest.
    Returns None if there is nothing new to send.
    """
    global _last_error_digest_sent_at

    with _LOCK:
        if not _error_events:
            return None

        # Only include errors strictly after the last digest
        if _last_error_digest_sent_at is None:
            new_events = _error_events[-15:]  # on first run, show last handful
        else:
            new_events = [e for e in _error_events if e["time"] > _last_error_digest_sent_at]

        if not new_events:
            return None

        # Mark that we've digested up to now
        _last_error_digest_sent_at = now

    # Format digest outside the lock
    head_time = _format_est_timestamp(now)
    lines = [
        "⚠️ MoneySignalAI — Recent Bot Errors",
        "",
        head_time,
        "",
        "The following errors were recorded:",
    ]

    for e in new_events[-15:]:  # cap at 15 lines per message
        t_s = e["time"].strftime("%H:%M:%S")
        src = e["source"]
        msg = e["message"]
        # keep each line compact
        if len(msg) > 180:
            msg = msg[:177] + "..."
        lines.append(f"• {t_s} — {src}: {msg}")

    return "\n".join(lines)


def _build_heartbeat(now: datetime) -> Optional[str]:
    """
    Build a light heartbeat / health snapshot.

    Behavior:
      • Always allowed to build (no universe/errors required)
      • run_status_report() controls the frequency (startup + every ~10 minutes).
    """
    with _LOCK:
        universe = _universe_health
        one_hour_ago = now - timedelta(hours=1)
        recent_errors = [e for e in _error_events if e["time"] >= one_hour_ago]
        total_errors_1h = len(recent_errors)

        # attach recent run info (if any)
        recent_runs = consume_recent_bot_runs()

    head_time = _format_est_timestamp(now)
    lines = [
        "✅ MoneySignalAI — Heartbeat",
        "",
        head_time,
        "",
    ]

    # Universe info
    if universe is not None:
        cov_pct = universe["coverage"] * 100.0
        size = universe["size"]
        uni_time = universe["time"].strftime("%H:%M:%S")
        lines.append(f"📊 Universe: {size} tickers · coverage ≈ {cov_pct:.1f}% (as of {uni_time} EST)")
    else:
        lines.append("📊 Universe: no recent universe snapshot recorded.")

    # Errors last hour
    if total_errors_1h:
        lines.append(f"⚠️ Errors last 60m: {total_errors_1h}")
    else:
        lines.append("✅ Errors last 60m: none recorded.")

    # Recent run outcomes
    if recent_runs:
        lines.append("")
        lines.append("🧪 Recent bot runs:")
        for ts, bot_name, outcome in recent_runs[-20:]:
            t_s = ts.strftime("%H:%M:%S")
            icon = "✅" if outcome == "ok" else "⚠️"
            lines.append(f"• {t_s} — {bot_name}: {icon} {outcome}")
    else:
        lines.append("")
        lines.append("🧪 Recent bot runs: no entries logged this cycle.")

    return "\n".join(lines)


# --------------- MAIN ENTRYPOINT ---------------

async def run_status_report() -> None:
    """
    Called from main.py once per scan cycle.

    Priority:
      1) If there are new errors since the last digest → send error digest.
      2) Else:
          - If no heartbeat ever sent → send startup heartbeat immediately.
          - Else if ≥10 minutes since last heartbeat → send heartbeat.
      3) Else, do nothing (log that there was nothing to send).
    """
    now = now_est()
    global _last_heartbeat_sent_at, _startup_heartbeat_sent

    # 1) Try to send an error digest if there are new errors
    error_text = _build_error_digest(now)
    if error_text:
        _send_status_telegram(error_text)
        print("[status_report] sent error digest.")
        return

    # 2a) On first run after startup, always send a heartbeat
    if not _startup_heartbeat_sent:
        hb = _build_heartbeat(now)
        if hb:
            _send_status_telegram(hb)
            _startup_heartbeat_sent = True
            with _LOCK:
                _last_heartbeat_sent_at = now
            print("[status_report] sent startup heartbeat.")
            return

    # 2b) Periodic heartbeat every ~10 minutes
    with _LOCK:
        if _last_heartbeat_sent_at is None:
            elapsed_ok = True
        else:
            elapsed_ok = (now - _last_heartbeat_sent_at) >= timedelta(minutes=10)

    if elapsed_ok:
        hb = _build_heartbeat(now)
        if hb:
            _send_status_telegram(hb)
            with _LOCK:
                _last_heartbeat_sent_at = now
            print("[status_report] sent periodic heartbeat.")
            return

    # 3) Nothing to send this minute
    print("[status_report] No status to send at this minute.")