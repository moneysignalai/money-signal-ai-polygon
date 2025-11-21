# bots/status_report.py
#
# Central status / heartbeat bot for MoneySignalAI
#
# Exposes:
#   • record_bot_error(bot_name, exc)
#   • log_bot_run(bot_name, status)   # status in {"ok", "error"}
#   • run_status_report()             # async, scheduled from main.py
#
# Behavior:
#   • Tracks last run time + last status per bot in memory.
#   • Every few cycles, sends a heartbeat message to the STATUS chat:
#       - Overall status (OK / Errors)
#       - Per-bot last status + timestamp
#       - Recent error snippets
#   • Uses the same Telegram env vars as bots/shared.py.

from __future__ import annotations

import traceback
import time
from datetime import datetime
from typing import Dict, Any, Optional

import pytz

from bots.shared import (
    now_est,   # returns nice EST time string
)

# We import the private helper; that's fine in our own codebase.
from bots.shared import _send_status  # type: ignore[attr-defined]

eastern = pytz.timezone("US/Eastern")

# ---------------- STATE ----------------

# bot_name -> {"last_status": "ok"|"error", "last_time": str, "last_error": Optional[str]}
_BOT_STATE: Dict[str, Dict[str, Any]] = {}

# Recent error log (rolling)
_RECENT_ERRORS: list[Dict[str, Any]] = []

# Last time (epoch seconds) we sent a heartbeat
_LAST_HEARTBEAT_TS: Optional[float] = None

# Minimum seconds between heartbeats
HEARTBEAT_INTERVAL_SEC = 10 * 60  # 10 minutes; adjust if you want


def _now_ts() -> float:
    return time.time()


def _append_error(bot: str, exc: BaseException) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    entry = {
        "bot": bot,
        "time": now_est(),
        "error": str(exc),
        "trace": tb[-800:],  # keep last ~800 chars
    }
    _RECENT_ERRORS.append(entry)
    # keep last 30 errors max
    if len(_RECENT_ERRORS) > 30:
        del _RECENT_ERRORS[:-30]


# ---------------- PUBLIC API ----------------

def record_bot_error(bot_name: str, exc: BaseException) -> None:
    """
    Called by main.run_all_once() when a bot raises an exception.
    We just update state and keep going; status will be summarized
    in the next heartbeat.
    """
    global _BOT_STATE
    ts_str = now_est()
    _BOT_STATE.setdefault(bot_name, {})
    _BOT_STATE[bot_name].update(
        {
            "last_status": "error",
            "last_time": ts_str,
            "last_error": str(exc),
        }
    )
    _append_error(bot_name, exc)


def log_bot_run(bot_name: str, status: str) -> None:
    """
    Called by main.run_all_once() after a bot completes its cycle.

    status:
        "ok"    -> bot completed without crashing
        "error" -> bot crashed (main already called record_bot_error)
    """
    global _BOT_STATE
    ts_str = now_est()
    _BOT_STATE.setdefault(bot_name, {})
    _BOT_STATE[bot_name].update(
        {
            "last_status": status,
            "last_time": ts_str,
        }
    )


# ---------------- HEARTBEAT ----------------

def _build_status_message() -> str:
    """
    Build a compact heartbeat / status summary for Telegram.
    """
    if not _BOT_STATE:
        return f"📊 Status Report — {now_est()}\n\nNo bot runs recorded yet."

    ok_bots = [b for b, s in _BOT_STATE.items() if s.get("last_status") == "ok"]
    err_bots = [b for b, s in _BOT_STATE.items() if s.get("last_status") == "error"]

    overall = "✅ ALL GOOD" if not err_bots else "⚠️ ERRORS DETECTED"

    lines = [
        f"📊 MoneySignalAI Status — {now_est()}",
        overall,
        "────────────",
        "🤖 Bot States:",
    ]

    # List each bot with last status + time
    for bot, info in sorted(_BOT_STATE.items()):
        status = info.get("last_status", "?")
        ts = info.get("last_time", "?")
        emoji = "✅" if status == "ok" else "⚠️"
        lines.append(f"{emoji} {bot}: {status.upper()} (last @ {ts})")

    # Append a small recent error section
    if _RECENT_ERRORS:
        lines.append("────────────")
        lines.append("🧯 Recent Errors (last few):")
        # show up to 5
        for err in _RECENT_ERRORS[-5:]:
            lines.append(
                f"• [{err['time']}] {err['bot']}: {err['error']}"
            )

    return "\n".join(lines)


async def run_status_report() -> None:
    """
    Called as a bot from main.run_all_once() on every scheduler cycle.

    We only send a real heartbeat message every HEARTBEAT_INTERVAL_SEC,
    but we update / maintain state every cycle.
    """
    global _LAST_HEARTBEAT_TS
    now_ts = _now_ts()

    # First run → force immediate heartbeat so you know it's alive
    if _LAST_HEARTBEAT_TS is None:
        _LAST_HEARTBEAT_TS = now_ts - HEARTBEAT_INTERVAL_SEC

    # If it's been long enough, send a heartbeat
    if now_ts - _LAST_HEARTBEAT_TS >= HEARTBEAT_INTERVAL_SEC:
        msg = _build_status_message()
        try:
            _send_status(msg)
        except Exception as e:
            # If status sending itself fails, just print; don't crash
            print("[status_report] ERROR sending heartbeat:", e)
        else:
            print("[status_report] Heartbeat sent.")
        _LAST_HEARTBEAT_TS = now_ts
    else:
        # No-op this cycle, just a debug print
        print("[status_report] Skipping heartbeat this cycle (interval not reached).")