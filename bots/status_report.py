import os
import pytz
from datetime import datetime
from bots.shared import send_status

eastern = pytz.timezone("US/Eastern")

def _should_send_daily_startup():
    """
    Send once per day at 8:55 AM EST.
    """
    now = datetime.now(eastern)
    if now.hour == 8 and now.minute == 55:
        return True
    return False


def _should_send_heartbeat():
    """
    Send every 2 hours at :00.
    """
    now = datetime.now(eastern)
    return now.minute == 0 and now.hour % 2 == 0


async def run_status_report():
    now = datetime.now(eastern)

    # DAILY STARTUP STATUS (8:55 AM EST)
    if _should_send_daily_startup():
        msg = (
            "📊 *Daily System Check — All Bots Online*\n\n"
            "• Premarket: Armed (4:00–9:29 AM)\n"
            "• Unusual Options: Armed (9:30–4:00)\n"
            "• Cheap 0DTE/3DTE: Armed (9:30–4:00)\n"
            "• ORB: Armed (9:45–11:00)\n"
            "• Gap: Armed (9:30–10:30)\n"
            "• Volume Monster: Armed (9:30–4:00)\n"
            "• Momentum Reversal: Armed (11:30–4:00)\n"
            "• Earnings: Armed (7 AM–10 PM)\n\n"
            "All systems nominal. Preparing for today's session. 🚀"
        )
        send_status(msg)
        print("[status_report] Sent daily startup status.")
        return

    # HEARTBEAT STATUS (every 2 hours)
    if _should_send_heartbeat():
        send_status(
            f"✅ System running normally — {now.strftime('%I:%M %p EST').lstrip('0')}"
        )
        print("[status_report] Heartbeat sent.")
        return

    print("[status_report] No status to send at this minute.")