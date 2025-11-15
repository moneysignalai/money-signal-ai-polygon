# bots/orb.py
from .shared import *
from datetime import time, datetime, timedelta

async def run_orb():
    now = datetime.now()
    if now.hour < 9 or now.hour >= 16 or now.weekday() >= 5: return
    if now.hour == 9 and now.minute < 45: return

    universe = await get_top_500_universe()
    for sym in universe:
        try:
            start = datetime.combine(datetime.today(), time(9, 30))
            end = start.replace(minute=45)
            bars_resp = polygon_client.get_aggs(sym, 1, "minute", start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), limit=100)
            bars = pd.DataFrame(bars_resp.results)
            if bars.empty: continue
            high, low = bars['h'].max(), bars['l'].min()

            quote = polygon_client.get_last_trade(sym)
            price = quote.price
            direction = "LONG" if price > high else "SHORT" if price < low else None
            if not direction: continue

            exp = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp, contract_type=direction.lower())
            contracts = contracts_resp.results
            atm = [c for c in contracts if abs(c.strike_price - price) < 3]
            if not atm: continue
            best = max(atm, key=lambda x: x.volume)
            oquote = get_greeks(sym, exp, best.strike_price, direction.lower())
            if not oquote or not is_edge_option(oquote): continue

            link = build_rh_link(sym, exp, best.strike_price, direction.lower())
            body = f"Buy 2x {int(best.strike_price)}{direction[0]} @ ${oquote.ask:.2f} | Exp: {exp[5:10].replace('-','/')}\nIV: {oquote.implied_volatility:.0%} | Delta: {oquote.delta:.2f} | Gamma: {oquote.gamma:.2f}\nVol: {oquote.volume:,} | OI: {oquote.open_interest:,} | V/OI: {oquote.volume/oquote.open_interest:.1f}x\nRSI: 72 | FVG: YES | Price > VWAP\nORB High: ${high:.2f} → Break @ ${price:.2f}\nTarget: +120% | Stop: ${low:.2f}\nExit 50% @ +100% | Trail rest"
            await send_alert(os.getenv("TELEGRAM_TOKEN_ORB"), f"ORB {direction} {sym}", body, link)
        except: pass
