# bots/debug_status_ping.py
#
# Simple status-channel heartbeat tester.
# Uses the shared status-reporting pipe (TELEGRAM_TOKEN_STATUS / TELEGRAM_CHAT_STATUS)
# via bots.shared.report_status_error.
#
# If you see these in your STATUS chat, that channel is wired correctly.

import os
import time

from bots.shared import now_est, report_status_error

# How often we’re allowed to send a ping (in seconds)
DEBUG_STATUS_PING_INTERVAL_SEC = int(os.getenv("DEBUG_STATUS_PING_INTERVAL_SEC", "600"))  # default: 10 min

_last_status_ping_ts: float | None = None


async def run_debug_status_ping():
    """
    Called every scheduler cycle from main.py.

    Logic:
      • If we've never sent a status ping → send one.
      • Else only send if at least DEBUG_STATUS_PING_INTERVAL_SEC seconds have passed.
    """
    global _last_status_ping_ts

    now_ts = time.time()

    # First run or interval passed → send a ping
    if _last_status_ping_ts is None or (now_ts - _last_status_ping_ts) >= DEBUG_STATUS_PING_INTERVAL_SEC:
        ts_str = now_est()  # already a human-friendly EST string

        # We intentionally go through report_status_error so it uses the same
        # status channel + fallback logic as all other internal helpers.
        msg = (
            "🧪 DEBUG STATUS PING\n"
            f"Timestamp: {ts_str}\n"
            "If you can see this, TELEGRAM_TOKEN_STATUS / TELEGRAM_CHAT_STATUS\n"
            "are correctly configured (or falling back to ALERTS if unset)."
        )

        # 'debug_status_ping' is the bot label; msg is the body.
        report_status_error("debug_status_ping", msg)

        _last_status_ping_ts = now_ts
    else:
        # Too soon since last ping → skip silently
        return
