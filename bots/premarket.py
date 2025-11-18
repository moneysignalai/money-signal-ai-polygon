# bots/premarket.py — ELITE: ONLY +8% WITH VOLUME
from .shared import send_alert, client
from datetime import datetime

async def run_premarket():
    now = datetime.now()
    if now.hour < 4 or now.hour >= 9 or now.minute % 12 != 0:
        return

    try:
        movers = client.get_snapshot_gainers_losers(direction="gainers", market_type="stocks")
        for m in movers[:20]:
            if m.change_percent >= 8.0 and m.volume >= 500_000:
                await send_alert("premarket", m.ticker, m.last_quote.ask, 0,
                                 f"PRE-MARKET RUNNER +{m.change_percent:.1f}%\n"
                                 f"Volume {m.volume:,} · Gapper setting up")
    except:
        pass