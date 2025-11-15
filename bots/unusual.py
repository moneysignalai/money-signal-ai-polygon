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
            exp = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp)
            contracts = contracts_resp.results
            for c in contracts:
                quote = get_greeks(sym, exp, c.strike_price, c.contract_type)
                if not quote or quote.volume < 5000: continue
                if quote.volume < 3 * quote.open_interest: continue
                if not is_edge_option(quote): continue
                link = build_rh_link(sym, exp, c.strike_price, c.contract_type)
                body = f"UNUSUAL {c.contract_type.upper()} {sym}\nVol: {quote.volume:,} (3x OI)\nIV: {quote.implied_volatility:.0%} | Delta: {quote.delta:.2f}\nEntry: Momentum | Exit: 50% @ +100%"
                await send_alert(os.getenv("TELEGRAM_TOKEN_UNUSUAL"), f"UNUSUAL {c.contract_type.upper()} {sym}", body, link)
                break
        except: pass
