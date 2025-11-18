from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
async def run_unusual():
    try:
        trades = client.list_trades("O:*", limit=800)
        for t in trades:
            if t.size > 500:
                await send_alert("unusual", t.underlying_ticker, t.price, 0, f"SWEEP {t.size}x")
    except: pass