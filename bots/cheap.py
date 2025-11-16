# bots/cheap.py
from .shared import *
from datetime import datetime

async def run_cheap():
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
                if not quote or quote.ask >= 1.0 or quote.volume < 1000: continue
                if not is_edge_option(quote): continue
                if now.hour > 14 and exp == datetime.today().strftime("%Y-%m-%d"): continue  # Time Decay
                link = build_rh_link(sym, exp, c.strike_price, c.contract_type)
                confidence = get_confidence_score(quote.volume/quote.open_interest, quote.gamma, quote.implied_volatility, False, True)
                body = f"Buy 3x {int(c.strike_price)}{c.contract_type[0]} @ ${quote.ask:.2f} | Exp: {exp[5:10].replace('-','/')}\nIV: {quote.implied_volatility:.0%} | Delta: {quote.delta:.2f} | Gamma: {quote.gamma:.2f}\nVol: {quote.volume:,} | OI: {quote.open_interest:,} | V/OI: {quote.volume/quote.open_interest:.1f}x\nEntry: Scalp | Exit: 50% @ +80% | 50% @ +150% | Trail"
                await send_alert(os.getenv("TELEGRAM_TOKEN_DEAL"), f"DEAL {sym} {c.contract_type.upper()}", body, link, confidence)
                break
        except: pass
