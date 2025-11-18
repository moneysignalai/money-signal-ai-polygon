from .shared import send_alert
from .helpers import client   # <-- ADD THIS LINE
from datetime import datetime, timedelta
async def run_cheap():
    try:
        contracts = client.list_options_contracts(contract_type="call", limit=1000)
        seen = set()
        for c in contracts:
            t = c.underlying_ticker
            if not t or t in seen: continue
            seen.add(t)
            bars = client.get_aggs(t, 1, "day", limit=30)
            if len(bars) < 10: continue
            df = __import__("pandas").DataFrame([b.__dict__ for b in bars])
            price = df["close"].iloc[-1]
            if price > 150: continue
            rvol = df["volume"].iloc[-1] / df["volume"].iloc[:-1].mean()
            if rvol < 1.1: continue
            await send_alert("cheap", t, price, round(rvol,2), f"0DTE–7DTE CALL · IV {c.implied_volatility:.0%}")
    except: pass