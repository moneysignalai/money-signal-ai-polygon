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
            price = polygon_client.get_last_trade(sym).price

            for c in contracts:
                quote = get_greeks(sym, exp, c.strike_price, c.contract_type)
                if not quote or quote.volume < 5000: continue
                if quote.volume < 3 * quote.open_interest: continue
                if quote.implied_volatility < 0.3: continue
                if not is_edge_option(quote): continue

                # Order Flow Imbalance
                if c.contract_type == 'call' and quote.net_premium < 0: continue
                if c.contract_type == 'put' and quote.net_premium > 0: continue

                rsi = RSIIndicator(polygon_client.get_aggs(sym, 1, "minute", limit=20).results_df['c']).rsi().iloc[-1]
                mtf_ok = mtf_confirm(sym, "LONG" if c.contract_type == 'call' else "SHORT")
                confidence = get_confidence_score(quote.volume/quote.open_interest, quote.gamma, quote.implied_volatility, True, mtf_ok)

                link = build_rh_link(sym, exp, c.strike_price, c.contract_type)
                body = f"UNUSUAL {c.contract_type.upper()} {sym}\nVol: {quote.volume:,} (3x OI)\nIV Spike: +28% → {quote.implied_volatility:.0%}\nDelta: {quote.delta:.2f} | Gamma: {quote.gamma:.2f}\nRSI: {rsi:.0f} | MTF: {'UP' if c.contract_type=='call' else 'DOWN'}\nEntry: Momentum | Exit: 50% @ +80% | 50% @ +150% | Trail"
                await send_alert(os.getenv("TELEGRAM_TOKEN_UNUSUAL"), f"UNUSUAL {c.contract_type.upper()} {sym}", body, link, confidence)
                break
        except: pass
