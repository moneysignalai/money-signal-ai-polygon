from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from .helpers import get_top_volume_stocks
async def run_squeeze():
    for sym in get_top_volume_stocks(120):
        try:
            bars = client.get_aggs(sym, 1, "day", limit=2)
            if len(bars) == 2 and bars[-1].close > bars[-2].close * 1.06:
                p = client.get_last_trade(sym).price
                await send_alert("squeeze", sym, p, 3.0, "SQUEEZE CANDIDATE")
        except: continue