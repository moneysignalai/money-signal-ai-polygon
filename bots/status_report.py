# bots/status_report.py — system heartbeat + daily status + error digests

from __future__ import annotations

import pytz
from datetime import datetime, date
from typing import List, Tuple

from bots.shared import send_status

eastern = pytz.timezone("US/Eastern")

# State flags so we don't spam Telegram
_PROCESS_RESTART_SENT = False
_LAST_STARTUP_DAY: date | None = None
_LAST_HEARTBEAT_KEY: str | None = None  # e.g. "2025-11-20-10" for 10:00

# Error buffer: holds recent bot errors until the next status_report tick
_ERROR_BUFFER: List[Tuple[datetime, str, str]] = []


# ---------------- INTERNAL HELPERS ----------------


def _should_send_restart(now_et: datetime) -> bool:
    """
    Only once per process boot. As soon as the scanner loop starts,
    we send a single 'bot restarted' message.
    """
    global _PROCESS_RESTART_SENT
    if _PROCESS_RESTART_SENT:
        return False
    _PROCESS_RESTART_SENT = True
    return True


def _should_send_daily_startup(now_et: datetime) -> bool:
    """
    Send the big 'system armed' status once per calendar day at 08:55 AM EST.
    This runs *inside* the 60-second scan loop.
    """
    global _LAST_STARTUP_DAY

    if not (now_et.hour == 8 and now_et.minute == 55):
        return False

    today = now_et.date()
    if _LAST_STARTUP_DAY == today:
        return False

    _LAST_STARTUP_DAY = today
    return True


def _should_send_heartbeat(now_et: datetime) -> bool:
    """
    Lightweight heartbeat: every 2 hours on the hour between 10:00 and 20:00 EST.
    Example: 10:00, 12:00, 14:00, 16:00, 18:00, 20:00.
    """
    global _LAST_HEARTBEAT_KEY

    # Only on the top of the hour
    if now_et.minute != 0:
        return False

    # Only during the active trading / after-hours window
    if not (10 <= now_et.hour <= 20):
        return False

    key = f"{now_et.date()}-{now_et.hour}"
    if _LAST_HEARTBEAT_KEY == key:
        return False

    _LAST_HEARTBEAT_KEY = key
    return True


def _fmt_now(now_et: datetime) -> str:
    """Nice human-readable EST timestamp for messages."""
    return now_et.strftime("%I:%M %p · %b %d").lstrip("0") + " EST"


# ---------------- ERROR CAPTURE API ----------------


def record_bot_error(bot_name: str, error: Exception | str) -> None:
    """
    Called from main.py whenever a bot throws.

    We store the bot name + error message + timestamp in an in-memory buffer.
    The next time run_status_report() runs, it will pick these up and send a
    single Telegram digest summarizing the recent errors.
    """
    global _ERROR_BUFFER
    now_et = datetime.now(eastern)

    if isinstance(error, Exception):
        msg = f"{type(error).__name__}: {error}"
    else:
        msg = str(error)

    _ERROR_BUFFER.append((now_et, bot_name, msg))

    # Keep only last 50 errors to avoid unbounded growth
    if len(_ERROR_BUFFER) > 50:
        _ERROR_BUFFER = _ERROR_BUFFER[-50:]


def _consume_recent_errors(max_items: int = 5) -> List[str]:
    """
    Return up to max_items recent errors as formatted lines, then clear the buffer.
    """
    global _ERROR_BUFFER
    if not _ERROR_BUFFER:
        return []

    recent = _ERROR_BUFFER[-max_items:]
    _ERROR_BUFFER = []

    lines: List[str] = []
    for ts, bot, msg in recent:
        t = ts.strftime("%H:%M:%S")
        lines.append(f"{t} — {bot}: {msg}")
    return lines


# ---------------- MAIN ENTRYPOINT ----------------


async def run_status_report():
    """
    Entry point called from main.py once per scan cycle (about every 60 seconds).

    Decides *whether* to send:
      - a restart notice (once per process),
      - a daily 08:55 AM system status blast,
      - an error digest if any bots have thrown recently,
      - or a heartbeat every 2 hours,

    and posts via bots.shared.send_status(), which uses TELEGRAM_TOKEN_STATUS /
    TELEGRAM_CHAT_STATUS if set (otherwise falls back to the main alert bot).
    """
    now_et = datetime.now(eastern)

    # 1) Restart notice (only once per boot)
    if _should_send_restart(now_et):
        msg = (
            "⚡ *MoneySignalAI has restarted*\n\n"
            f"{_fmt_now(now_et)}\n\n"
            "The multi-bot scanner just booted (deploy / restart).\n"
            "All core modules are loading and will begin scanning on the next cycle."
        )
        send_status(msg)
        print("[status_report] Sent restart notice.")
        return

    # 2) Daily pre-market system status at 08:55 AM EST
    if _should_send_daily_startup(now_et):
        msg = (
            "*MoneySignalAI — SYSTEM STATUS*\n\n"
            f"{_fmt_now(now_et)}\n\n"
            "Multi-bot scanner is *armed* for today. Core modules:\n"
            "• Premarket Gap + Volume\n"
            "• Regular Session Gaps\n"
            "• Opening Range Breakouts (ORB)\n"
            "• Volume Monster\n"
            "• Cheap 0–5 DTE Options\n"
            "• Unusual Options Sweeps\n"
            "• Whale Flow\n"
            "• Short Squeeze Pro\n"
            "• Earnings Move + Fundamentals\n"
            "• Momentum Reversal\n"
            "• Swing Pullback\n"
            "• Panic Flush\n"
            "• Trend Rider (Daily Breakouts)\n"
            "• IV Crush (Post-Earnings)\n"
            "• Dark Pool Radar\n\n"
            "*Typical hunt windows (EST):*\n"
            "• 04:00–09:30 — Premarket, Dark Pool Radar\n"
            "• 09:30–10:30 — Gap & Go, ORB, Volume spikes\n"
            "• 09:30–16:00 — Cheap, Unusual, Whales, Squeeze, Momentum, Panic, Swing\n"
            "• 15:30–20:15 — Trend Rider, Dark Pool Radar, late Earnings/IV moves\n\n"
            "Everything is armed and watching the tape for:\n"
            "• Explosive volume\n"
            "• Big options flow\n"
            "• Key earnings movers\n"
            "• Dark pool clusters\n"
            "• High-probability reversals & breakouts\n\n"
            "You focus on execution.\n"
            "*Let the bots watch the market. ⚡*"
        )
        send_status(msg)
        print("[status_report] Sent daily startup status.")
        return

    # 3) Error digest: if any bots have thrown since the last tick, surface them ASAP
    error_lines = _consume_recent_errors(max_items=5)
    if error_lines:
        msg = (
            "⚠️ *MoneySignalAI — Recent Bot Errors*\n\n"
            f"{_fmt_now(now_et)}\n\n"
            "The following errors were recorded:\n"
            + "\n".join(f"• {line}" for line in error_lines)
        )
        send_status(msg)
        print(f"[status_report] Sent error digest with {len(error_lines)} item(s).")
        return

    # 4) Heartbeat (simple health ping so you know the loop is alive)
    if _should_send_heartbeat(now_et):
        hb = now_et.strftime("%I:%M %p EST").lstrip("0")
        send_status(f"✅ System running normally — {hb}")
        print("[status_report] Heartbeat sent.")
        return

    # 5) Nothing scheduled right now
    print("[status_report] No status to send at this minute.")