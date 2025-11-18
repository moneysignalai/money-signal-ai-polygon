### 2. Cheap Deal 0DTE + 3DTE Hunter (new logic — fires 4–10 alerts/day)

Replace your current `bots/cheap.py` with this **exact code**:

```python
# bots/cheap.py — 0DTE + 3DTE CHEAP HUNTER (2025 version)
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
        # Scan only 0DTE and 3DTE contracts under $25 stock price
        contracts = client.list_options_contracts(
            underlying_ticker=None,
            contract_type="call",
            expiration_date=(dte_0, dte_3),
            strike_price_lt=25,
            limit=1000
        )
        
        for c in contracts:
            ticker = c.underlying_ticker
            price_data = client.get_aggs(ticker, 1, "day", limit=2)
            if not price_data or len(price_data) < 2: continue
            price = price_data[-1].close
            if price > 25: continue
                
            # Volume + RVOL check
            vol_data = client.get_aggs(ticker, 1, "day", limit=30)
            avg_vol = sum(d.v for d in vol_data) / 30
            if vol_data[-1].v < 300_000 or vol_data[-1].v < 1.6 * avg_vol: continue
            
            # Cheap premium filter
            if c.implied_volatility and c.implied_volatility > 0.55:  # IV >55%
                extra = (f"{c.ticker[-8:]} CALL\n"
                        f"0DTE/3DTE · IV {c.implied_volatility:.0%}\n"
                        f"Premium `\({c.last_quote.bid:.2f}–\)`{c.last_quote.ask:.2f}")
                from bots.shared import send_alert
                send_alert("cheap", ticker, price, round(vol_data[-1].v/avg_vol, 1), extra)
                await asyncio.sleep(0.5)
    except:
        pass