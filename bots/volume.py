# bots/volume.py
from .shared import *
from datetime import datetime

async def run_volume():
    now = datetime.now()
    if now.hour != 9 or now.minute != 31 or now.weekday() >= 5: return

    universe = await get_top_500_universe()
    top_volume = []
    for sym in universe:
        try:
            bars = polygon_client.get_aggs(sym, 1, "minute", limit=1)
            vol = bars.results[0].volume
            if vol > 5_000_000:
                vwap = bars.results[0].vwap
                price = polygon_client.get_last_trade(sym).price
                if abs(price - vwap) / vwap < 0.01:  # Within 1% of VWAP
                    top_volume.append((sym, vol))
        except: pass

    top_volume = sorted(top_volume, key=lambda x: x[1], reverse=True)[:25]  # ← TOP 25
    body = f"*FLOW LEADERS (TOP 25) | {now.strftime('%H:%M')} AM EST*\n"
    for i, (s, v) in enumerate(top_volume):
        body += f"{i+1}. {s}: {v:,} shares (at VWAP)\n"
    if not top_volume:
        body += "No high-volume leaders yet."
    await send_alert(os.getenv("TELEGRAM_TOKEN_FLOW"), "FLOW LEADERS", body)
