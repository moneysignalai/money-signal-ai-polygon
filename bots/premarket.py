from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from datetime import datetime
async def run_premarket():
    n = datetime.now()
    if n.hour >= 9 or n.hour < 4 or n.minute % 15 != 0: return
    try:
        movers = client.get_snapshot_gainers_losers("gainers", "stocks")
        for m in movers[:15]:
            if m.change_percent > 8:
                await send_alert("premarket", m.ticker, m.last_quote.ask, 0, f"+{m.change_percent:.1f}%")
    except: pass