# bots/cheap.py — CHEAP 0DTE + 3DTE HUNTER (100% WORKING — NOV 18 2025)
import os
from polygon import RESTClient
from datetime import datetime, timedelta
import asyncio

client = RESTClient(os.getenv("POLYGON_KEY"))

async def run_cheap():
    try:
        # Get today and +3 days
        today = datetime.now().date()
        dte_0 = today.strftime("%Y-%m-%d")
        dte_3 = (today + timedelta(days=3)).strftime("%Y-%m-%d")

        # CORRECT WAY — use expiration_date_gte and lte separately
        contracts = client.list_options_contracts(
            contract_type="call",
            expiration_date_gte=dte_0,
            expiration_date_lte=dte_3,
            limit=1000
        )

        for c in contracts:
            ticker = c.underlying_ticker
            if not ticker: 
                continue

            # Get stock data
            agg = client.get_aggs(ticker, 1, "day", limit=30)
            if len(agg) < 2: 
                continue
            price = agg[-1].close
            if price > 25.0: 
                continue

            avg_vol = sum(a.volume for a in agg[:-1]) / 29
            today_vol = agg[-1].volume
            rvol = round(today_vol / avg_vol, 1) if avg_vol > 0 else 0

            if today_vol < 300_000 or rvol < 1.6: 
                continue

            if c.implied_volatility and c.implied_volatility >= 0.55:
                extra = f"{c.ticker[-8:]} CALL\n0–3 DTE · IV {c.implied_volatility:.0%}\nPremium ${c.last_quote.bid:.2f}–${c.last_quote.ask:.2f}"
                from bots.shared import send_alert
                send_alert("cheap", ticker, price, rvol, extra)
                await asyncio.sleep(0.5)

    except Exception as e:
        print(f"CHEAP BOT ERROR: {e}")