# bots/earnings.py — FINAL WORKING VERSION
from .shared import send_alert, client
from .helpers import get_top_volume_stocks
import yfinance as yf
from datetime import datetime, timedelta

async def run_earnings():
    # Only run during market hours
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 8 or now.hour >= 17:
        return

    # Scan top volume stocks
    tickers = get_top_volume_stocks(100)

    for sym in tickers[:80]:  # check top 80
        try:
            ticker = yf.Ticker(sym)
            cal = ticker.calendar
            if cal is None or cal.empty:
                continue
            earnings_date = cal.iloc[0, 0]  # next earnings date
            days_until = (earnings_date.date() - now.date()).days
            if 0 <= days_until <= 7:  # earnings this week or next
                price = client.get_last_trade(sym).price
                await send_alert(
                    "earnings", sym, price, 0,
                    f"EARNINGS IN {days_until} DAY{'S' if days_until != 1 else ''}\n"
                    f"Date: {earnings_date.strftime('%b %d')}\n"
                    f"Watch for big move"
                )
        except:
            continue