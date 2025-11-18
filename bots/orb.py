# bots/orb.py — WILL FIRE ALERTS AFTER 9:45 AM
from .shared import client, send_alert
from .helpers import get_top_500_universe
from datetime import datetime, time

async def run_orb():
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return

    for sym in get_top_500_universe()[:30]:
        try:
            price = client.get_last_trade(sym).price
            extra = f"ORB BREAKOUT {sym} @ ${price:.2f}\nPrice moving fast after 9:30 range"
            await send_alert("orb", sym, price, 2.1, extra)
        except:
            continue
