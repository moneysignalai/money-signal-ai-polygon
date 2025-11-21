# bots/debug_ping.py
#
# Simple heartbeat / connectivity tester.
# Sends a tiny alert to your normal ALERTS chat at most once every N minutes.
#
# This ignores Polygon, market hours, etc. Purely tests:
#   • TELEGRAM_TOKEN_ALERTS
#   • TELEGRAM_CHAT_ALL
#   • main.py scheduler + async wiring
#
# If you see this in Telegram, your pipe is good.

import os
import time

from bots.shared import send_alert, now_est

# How often we’re allowed to send a ping (in seconds)
PING_INTERVAL_SEC = int(os.getenv("DEBUG_PING_INTERVAL_SEC", "600"))  # default 10 minutes

_last_ping_ts: float | None = None


async def run_debug_ping():
    """
    Called every scheduler cycle from main.py.

    Logic:
      • If we've never sent a ping → send one.
      • Else only send if at least PING_INTERVAL_SEC has passed.
    """
    global _last_ping_ts

    now_ts = time.time()

    # First run → force send
    if _last_ping_ts is None or (now_ts - _last_ping_ts) >= PING_INTERVAL_SEC:
        ts_str = now_est()  # already a nice EST string
        extra = (
            f"🧪 DEBUG PING\n"
            f"🕒 {ts_str}\n"
            "────────────\n"
            "If you can see this, TELEGRAM_TOKEN_ALERTS + TELEGRAM_CHAT_ALL\n"
            "are correctly configured and the scheduler is running."
        )

        # bot_name: "debug_ping", symbol: "PING", last_price and rvol are cosmetic
        send_alert("debug_ping", "PING", last_price=0.0, rvol=0.0, extra=extra)
        _last_ping_ts = now_ts
    else:
        # Do nothing this cycle
        pass