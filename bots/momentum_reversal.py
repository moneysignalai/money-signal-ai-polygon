# bots/momentum_reversal.py — FINAL WORKING VERSION
from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from .helpers import get_top_volume_stocks
from datetime import datetime

async def run_momentum_reversal():
    # Only during regular trading hours
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 9 or now.hour >= 16:
        return

    tickers = get_top_volume_stocks(120)

    for sym in tickers:
        try:
            # Get today's bar
            bars = client.get_aggs(sym, 1, "day", limit=5)
            if len(bars) < 2:
                continue
            today = bars[-1]
            yesterday = bars[-2]

            price = client.get_last_trade(sym).price
            change_today = (today.close - today.open) / today.open * 100

            # Strong move up + pulling back OR strong down + bouncing
            if (change_today > 8 and price < today.high * 0.98) or \
               (change_today < -8 and price > today.low * 1.02):
                direction = "REVERSAL UP" if change_today < 0 else "REVERSAL DOWN"
                await send_alert(
                    "momentum", sym, price, 3.2,
                    f"{direction} · {change_today:+.1f}% today\n"
                    f"Strong momentum + reversal signal"
                )
        except:
            continue