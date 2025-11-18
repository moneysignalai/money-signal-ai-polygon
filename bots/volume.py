# bots/volume.py — ELITE: ONLY TOP 1-MINUTE VOLUME SPIKES
from .shared import send_alert, client
from .helpers import get_top_volume_stocks
from datetime import datetime

async def run_volume():
    if datetime.now().minute % 8 != 0:  # every 8 min
        return

    top_stocks = get_top_volume_stocks(200)
    for sym in top_stocks:
        try:
            bar = client.get_aggs(sym, 1, "minute", limit=1)
            if not bar or len(bar) == 0:
                continue
            vol_1min = bar[0].volume
            if vol_1min > 8_000_000:  # 8M+ in one minute = insane
                price = client.get_last_trade(sym).price
                await send_alert("volume", sym, price, 0,
                                 f"MONSTER 1-MIN VOLUME\n{vol_1min:,} shares · Top leader right now")
        except:
            continue