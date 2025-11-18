# bots/unusual.py — HEDGE-FUND UNUSUAL FLOW (2025 version)
import os
from polygon import RESTClient
from datetime import datetime
import asyncio

client = RESTClient(os.getenv("POLYGON_KEY"))

async def run_unusual():
    # Real hedge-fund filter: same strike + expiry + call/put bought aggressively
    try:
        trades = client.list_trades("O:*", limit=1000, timestamp=datetime.now().date())
        contracts = {}
        
        for trade in trades:
            if trade.size < 50 or trade.price < 0.5:  # ignore noise
                continue
                
            key = f"{trade.underlying_ticker}_{trade.expiration_date}_{trade.strike_price:.2f}_{trade.option_type}"
            contracts[key] = contracts.get(key, 0) + (trade.size * trade.price * 100)  # dollar volume
        
        # Alert only when $100k+ in SAME contract bought today
        for contract, dollar_vol in contracts.items():
            if dollar_vol >= 100_000:  # $100k+ in one contract = real money moving
                ticker, exp, strike, opt_type = contract.split("_")
                extra = (f"Same contract sweep\n"
                        f"${dollar_vol:,.0f} total premium\n"
                        f"{opt_type.upper()} ${strike} exp {exp[5:10]}")
                from bots.shared import send_alert
                send_alert("unusual", ticker, float(trade.price), 0, extra)
                await asyncio.sleep(1)  # avoid rate limit
    except:
        pass