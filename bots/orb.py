from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from .helpers import get_top_volume_stocks
from datetime import datetime
async def run_orb():
    n = datetime.now()
    if n.weekday() >= 5 or n.hour < 9 or (n.hour == 9 and n.minute < 35): return
    for sym in get_top_volume_stocks(120):
        try:
            p = client.get_last_trade(sym).price
            await send_alert("orb", sym, p, 2.5, "ORB BREAKOUT")
        except: continue