from .shared import send_alert
from .helpers import get_top_volume_stocks
from datetime import datetime
async def run_gap():
    n = datetime.now()
    if not (n.hour == 9 and 30 <= n.minute <= 55): return
    for sym in get_top_volume_stocks(100):
        try:
            p = client.get_last_trade(sym).price
            await send_alert("gap", sym, p, 2.0, "GAP SETUP")
        except: continue