import pytz
from datetime import datetime
from bots.shared import send_status

eastern = pytz.timezone("US/Eastern")

# One-per-process flag so we only send the restart notice once
_PROCESS_RESTART_ANNOUNCED = False


def _should_send_daily_startup(now_et: datetime) -> bool:
    """
    Send once per day at 8:55 AM EST.
    """
    return now_et.hour == 8 and now_et.minute == 55


def _should_send_heartbeat(now_et: datetime) -> bool:
    """
    Send a simple heartbeat every 2 hours on the hour (10:00, 12:00, 14:00, ...).
    Adjust if your scheduler runs less frequently.
    """
    return now_et.minute == 0 and now_et.hour % 2 == 0


async def run_status_report():
    """
    Central status/health bot.

    Responsibilities:
      • On process start: announce restart.
      • 08:55 EST: full "all bots armed" schedule message.
      • Every 2 hours on the hour: short heartbeat.
    """
    global _PROCESS_RESTART_ANNOUNCED

    now_et = datetime.now(eastern)

    # 1) One-time restart notification when this process starts
    if not _PROCESS_RESTART_ANNOUNCED:
        _PROCESS_RESTART_ANNOUNCED = True
        restart_msg = (
            "🟢 *MoneySignal AI — Process Restarted*\n\n"
            f"Instance booted at: {now_et.strftime('%I:%M %p EST · %b %d').lstrip('0')}\n"
            "If you did not intentionally redeploy or restart, treat this as a soft health check.\n"
        )
        send_status(restart_msg)
        print("[status_report] Restart announcement sent.")
        # Don’t `return` — we may also want to send startup/heartbeat on the same minute

    # 2) Daily startup schedule and “all bots armed” view (08:55 EST)
    if _should_send_daily_startup(now_et):
        msg = (
            "📊 *Daily System Check — All Bots Online*\n\n"
            "• Premarket Runner: 4:00–9:29 AM\n"
            "• Gap Bot: 9:30–10:30 AM\n"
            "• ORB + FVG Bot: 9:45–11:00 AM\n"
            "• Volume Monster: 9:30 AM–4:00 PM\n"
            "• Cheap 0DTE/3DTE Hunter: 9:30 AM–4:00 PM\n"
            "• Unusual Options Sweeps (Calls + Puts): 9:30 AM–4:00 PM\n"
            "• Short Squeeze Pro: 9:30 AM–4:00 PM\n"
            "• Momentum Reversal: 11:30 AM–4:00 PM\n"
            "• Earnings Catalyst: 7:00 AM–10:00 PM\n\n"
            "All modules armed and ready for today's session. 🚀"
        )
        send_status(msg)
        print("[status_report] Sent daily startup status.")
        return

    # 3) Heartbeat (every 2 hours on the hour)
    if _should_send_heartbeat(now_et):
        hb = now_et.strftime("%I:%M %p EST").lstrip("0")
        send_status(f"✅ System running normally — {hb}")
        print("[status_report] Heartbeat sent.")
        return

    # Nothing to send this minute
    print("[status_report] No status to send at this minute.")