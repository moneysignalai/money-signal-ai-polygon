# bots/cheap.py — CHEAP 0DTE + 3DTE HUNTER (2025 Elite Version)
import os
from polygon import RESTClient
from datetime import datetime, timedelta
import asyncio

client = RESTClient(os.getenv("POLYGON_KEY"))
today = datetime.now().date()
dte_0 = today.strftime("%Y-%m-%d")
dte_3 = (today + timedelta(days=3)).strftime("%Y-%m-%d")

async def run_cheap():
    try:
        contracts = client.list_options_contracts(
            contract_type="call",
            expiration_date=(dte_0, dte_3),
            as_of=today,
            limit=1000
        )

        for c in contracts:
            ticker = c.underlying_ticker
            if not ticker: 
                continue

            # Get stock price + volume
            agg = client.get_aggs(ticker, 1, "day", limit=30)
            if len(agg) < 2: 
                continue
            price = agg[-1].close
            if price > 25.0: 
                continue

            avg_vol = sum(a.volume for a in agg[:-1]) / 29
            today_vol = agg[-1].volume
            if today_vol < 300_000 or today_vol < 1.6 * avg_vol: 
                continue

            if c.implied_volatility and c.implied_volatility >= 0.55:
                extra = f"{c.ticker[-8:]} CALL\n0–3 DTE · IV {c.implied_volatility:.0%}\nPremium ${c.last_quote.bid:.2f}–${c.last_quote.ask:.2f}"
                from bots.shared import send_alert
                send_alert("cheap", ticker, price, round(today_vol/avg_vol, 1), extra)
                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"CHEAP BOT ERROR: {e}")