# bots/unusual.py
from .shared import *
from datetime import datetime

async def run_unusual():
    now = datetime.now()
    if now.hour < 9 or now.hour >= 16 or now.weekday() >= 5: return
    if now.minute % 15 != 0: return

    universe = await get_top_500_universe()
    for sym in universe:
        try:
            expirations = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6)]
            for exp in expirations:
                contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp)
                contracts = contracts_resp.results
                price = polygon_client.get_last_trade(sym).price

                for c in contracts:
                    quote = get_greeks(sym, exp, c.strike_price, c.contract_type)
                    if not quote or quote.volume < 3000: continue
                    if quote.volume < 2 * quote.open_interest: continue        # ← loosened from 3x
                    if quote.implied_volatility < 0.2: continue               # ← loosened from 0.3
                    if not is_edge_option(quote): continue

                    mtf_ok = mtf_confirm