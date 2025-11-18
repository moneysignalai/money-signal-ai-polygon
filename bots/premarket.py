# bots/premarket.py — BONUS BOT (create this new file)
from .shared import client, send_alert
from datetime import datetime

async def run_premarket():
    now = datetime.now()
    if now.hour >= 9 or now.hour < 4: return
    if now.minute % 10 != 0: return

    movers = client.get_snapshot_gainers_losers(direction="gainers", market_type="stocks")
    for s in movers[:15]:
        try:
            if s.change_percent > 8:
                await send_alert("premarket", s.ticker, s.last_quote.ask, 0,
                                 f"PRE-MARKET RUNNER +{s.change_percent:.1f}% · Vol {s.volume:,}")
        except: continue
