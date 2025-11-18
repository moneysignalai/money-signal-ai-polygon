# bots/unusual.py — ELITE: ONLY $200k+ SAME-CONTRACT SWEEPS
from .shared import send_alert, client
import asyncio
from collections import defaultdict

async def run_unusual():
    try:
        trades = client.list_trades("O:*", limit=1000)  # last ~5–10 min
        sweeps = defaultdict(int)  # (ticker, exp, strike, type) → total premium

        for t in trades:
            if t.size < 80 or t.price < 0.8:  # ignore small/retail noise
                continue
            key = (t.underlying_ticker, t.expiration_date, t.strike_price, t.option_type)
            sweeps[key] += t.size * t.price * 100  # dollar premium

        for (ticker, exp, strike, opt_type), premium in sweeps.items():
            if premium >= 200_000:  # $200k+ in ONE contract = real money
                price = client.get_last_trade(ticker).price
                extra = (
                    f"UNUSUAL SWEEP DETECTED\n"
                    f"${premium:,.0f} premium · {opt_type.upper()} ${strike}\n"
                    f"Exp {exp[5:10].replace('-','/')} · Same-strike aggression"
                )
                await send_alert("unusual", ticker, price, 0, extra)
                await asyncio.sleep(0.8)  # avoid rate limit
    except:
        pass