from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from .helpers import get_top_volume_stocks
from datetime import datetime
async def run_volume():
    if datetime.now().minute % 10 != 0: return
    for sym in get_top_volume_stocks(150):
        try:
            v = client.get_aggs(sym, 1, "minute", limit=1)[0].volume
            if v > 5_000_000:
                p = client.get_last_trade(sym).price
                await send_alert("volume", sym, p, 0, f"VOLUME SPIKE {v:,}")
        except: continue