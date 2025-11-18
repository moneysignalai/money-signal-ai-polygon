# bots/volume.py — WILL FIRE EVERY 15 MINUTES
from .shared import client, send_alert
from .helpers import get_top_500_universe

async def run_volume():
    if datetime.now().minute % 15 != 0:
        return
    for sym in get_top_500_universe()[:20]:
        try:
            vol = client.get_aggs(sym, 1, "minute", limit=1)[0].volume
            if vol > 2_000_000:
                price = client.get_last_trade(sym).price
                await send_alert("volume", sym, price, 0, f"TOP VOLUME · {vol:,} shares in 1 min")
        except:
            continue
