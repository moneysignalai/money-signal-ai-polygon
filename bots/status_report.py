# bots/status_report.py
#
# Advanced Status / Heartbeat System for MoneySignalAI
#
# NEW CAPABILITIES:
#   • Per-bot analytics (scan counts, matches, alerts, runtime).
#   • Rolling error intel (last 30 errors + error categories).
#   • Running averages to evaluate scanner performance.
#   • Global analytics: total tickers scanned, alerts fired.
#   • Heartbeat shows top heavy-load bots and most error-prone bots.
#
# Existing API preserved:
#   record_bot_error(bot, exc)
#   log_bot_run(bot, status)
#   record_bot_stats(bot, *, scanned, matched, alerts, runtime)
#   async run_status_report()
#

from __future__ import annotations

import traceback
import time
from datetime import datetime
from typing import Dict, Any, Optional

import pytz
from bots.shared import now_est, _send_status  # private import OK inside repo

eastern = pytz.timezone("US/Eastern")

# ---------------- STATE STRUCTURES ----------------

# Per-bot state
_BOT_STATE: Dict[str, Dict[str, Any]] = {}

# Extended performance metrics (rolling)
#
# bot → {
#    "scanned": int,
#    "matched": int,
#    "alerts": int,
#    "runtime": float,
#    "history": [
#         {"scanned": n, "matched": m, "alerts": a, "runtime": s, "time": "..."},
#    ]
# }
_BOT_STATS: Dict[str, Dict[str, Any]] = {}

# Rolling error log
_RECENT_ERRORS: list[Dict[str, Any]] = []

# Rolling per-bot error counter
_BOT_ERROR_COUNTER: Dict[str, int] = {}

_LAST_HEARTBEAT_TS: Optional[float] = None
HEARTBEAT_INTERVAL_SEC = 10 * 60  # every 10 minutes


def _now_ts() -> float:
    return time.time()


# ---------------- ERROR CAPTURE ----------------

def _append_error(bot: str, exc: BaseException) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    category = "internal"
    msg = str(exc).lower()
    if "429" in msg or "rate" in msg:
        category = "polygon-rate-limit"
    elif "connection" in msg or "timeout" in msg:
        category = "network"
    elif "polygon" in msg:
        category = "polygon"

    entry = {
        "bot": bot,
        "time": now_est(),
        "category": category,
        "error": str(exc),
        "trace": tb[-900:],   # keep end of traceback
    }

    _RECENT_ERRORS.append(entry)
    if len(_RECENT_ERRORS) > 30:
        del _RECENT_ERRORS[:-30]

    _BOT_ERROR_COUNTER[bot] = _BOT_ERROR_COUNTER.get(bot, 0) + 1


# ---------------- PUBLIC API ----------------

def record_bot_error(bot_name: str, exc: BaseException) -> None:
    """Main.py calls this when a bot throws."""
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
    """Main.py calls this when a bot completes (ok/error)."""
    ts_str = now_est()
    _BOT_STATE.setdefault(bot_name, {})
    _BOT_STATE[bot_name].update(
        {
            "last_status": status,
            "last_time": ts_str,
        }
    )


def record_bot_stats(
    bot_name: str,
    *,
    scanned: int,
    matched: int,
    alerts: int,
    runtime: float,
) -> None:
    """
    Bots call this to submit analytics after each full cycle.
    (You will add 3–5 lines inside each bot.)
    """
    _BOT_STATS.setdefault(bot_name, {
        "scanned": 0,
        "matched": 0,
        "alerts": 0,
        "runtime": 0.0,
        "history": [],
    })

    # Update rolling totals
    st = _BOT_STATS[bot_name]
    st["scanned"] += scanned
    st["matched"] += matched
    st["alerts"] += alerts
    st["runtime"] += runtime

    st["history"].append({
        "time": now_est(),
        "scanned": scanned,
        "matched": matched,
        "alerts": alerts,
        "runtime": runtime,
    })

    # Keep last 40 runs
    if len(st["history"]) > 40:
        del st["history"][:-40]


# ---------------- HEARTBEAT CONSTRUCTION ----------------

def _build_stats_section() -> list[str]:
    if not _BOT_STATS:
        return ["📉 No scan statistics yet."]

    lines = ["📊 **Scanner Analytics:**"]

    total_scanned = sum(st["scanned"] for st in _BOT_STATS.values())
    total_matched = sum(st["matched"] for st in _BOT_STATS.values())
    total_alerts = sum(st["alerts"] for st in _BOT_STATS.values())

    lines.append(f"• Total scanned: **{total_scanned:,}**")
    lines.append(f"• Filter matches: **{total_matched:,}**")
    lines.append(f"• Alerts fired: **{total_alerts:,}**")

    # Top 3 scan-heavy bots
    heavy = sorted(_BOT_STATS.items(), key=lambda x: x[1]["scanned"], reverse=True)[:3]
    if heavy:
        lines.append("\n🥵 **Top 3 Heaviest Bots (by scans):**")
        for bot, st in heavy:
            lines.append(f"• {bot}: {st['scanned']:,} scanned")

    # Top 3 error-prone bots
    noisy = sorted(_BOT_ERROR_COUNTER.items(), key=lambda x: x[1], reverse=True)[:3]
    if noisy:
        lines.append("\n🔥 **Top 3 Noisiest Bots (errors):**")
        for bot, count in noisy:
            lines.append(f"• {bot}: {count} errors")

    return lines


def _build_status_message() -> str:
    lines = [
        f"📡 **MoneySignalAI Heartbeat** — {now_est()}",
        "────────────────────",
    ]

    ok = [b for b, s in _BOT_STATE.items() if s.get("last_status") == "ok"]
    err = [b for b, s in _BOT_STATE.items() if s.get("last_status") == "error"]

    overall = "✅ ALL SYSTEMS GOOD" if not err else "⚠️ ERRORS DETECTED"
    lines.append(overall)
    lines.append("")

    # Per-bot status
    lines.append("🤖 **Bot Status:**")
    for bot, info in sorted(_BOT_STATE.items()):
        emoji = "✅" if info.get("last_status") == "ok" else "⚠️"
        lines.append(f"{emoji} {bot}: {info.get('last_status','?').upper()} @ {info.get('last_time','?')}")

    lines.append("────────────────────")

    # Stats section
    lines.extend(_build_stats_section())
    lines.append("────────────────────")

    # Error section
    if _RECENT_ERRORS:
        lines.append("🧯 **Recent Errors:**")
        for err in _RECENT_ERRORS[-5:]:
            lines.append(f"• [{err['time']}] {err['bot']} ({err['category']}): {err['error']}")

    return "\n".join(lines)


# ---------------- HEARTBEAT LOOP ----------------

async def run_status_report() -> None:
    """Called every cycle from main.run_all_once()."""
    global _LAST_HEARTBEAT_TS
    now_ts = _now_ts()

    # Force immediate heartbeat at startup
    if _LAST_HEARTBEAT_TS is None:
        _LAST_HEARTBEAT_TS = now_ts - HEARTBEAT_INTERVAL_SEC

    # Send heartbeat
    if now_ts - _LAST_HEARTBEAT_TS >= HEARTBEAT_INTERVAL_SEC:
        msg = _build_status_message()
        try:
            _send_status(msg)
        except Exception as e:
            print("[status_report] FAILED sending heartbeat:", e)
        else:
            print("[status_report] Heartbeat sent.")
        _LAST_HEARTBEAT_TS = now_ts
    else:
        print("[status_report] Heartbeat skipped (interval).")
