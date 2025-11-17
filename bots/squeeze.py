# bots/squeeze.py
from .shared import *
from datetime import datetime

async def run_squeeze():
    now = datetime.now()
    if now.hour < 9 or now.hour >= 16 or now.weekday() >= 5: return
    if now.minute % 15 != 0: return

    universe = await get_top_500_universe()
    for sym in universe:
        try:
            short_resp = polygon_client.get_short_interest(sym, limit=1)
            short_data = short_resp.results[0] if short_resp.results else None
            if not short_data or short_data.short_interest / short_data.shares_outstanding < 0.18: continue  # loosened

            # REMOVED: float filter
            # if short_data.shares_outstanding > 100_000_000: continue

            quote = polygon_client.get_last_trade(sym)
            price = quote.price
            bars = polygon_client.get_aggs(sym, 1, "minute", limit=20)
            bars_df = pd.DataFrame(bars.results)
            if bars_df['c'].iloc[-1] < bars_df['c'].iloc[0] * 1.025: continue  # +2.5%
            if bars_df['v'].iloc[-1] < 3 * bars_df['v'][:-1].mean(): continue   # loosened

            exp = (datetime.today() + timedelta(days=1)).strftime("%Y-%m-%d")
            contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp, contract_type='call')
            contracts = contracts_resp.results
            atm = [c for c in contracts if abs(c.strike_price - price) < 3]
            if not atm: continue
            best = max(atm, key=lambda x: x.volume)
            oquote = get_greeks(sym, exp, best.strike_price, 'call')
            if not oquote or not is_edge_option(oquote): continue

            link = build_rh_link(sym, exp, best.strike_price, 'call')
            score = min(100, int((short_data.short_interest / short_data.shares_outstanding)*250 + 15))
            confidence = get_confidence_score(oquote.volume/oquote.open_interest, oquote.gamma, oquote.implied_volatility, True, True)
            body = f"SQUEEZE LONG {sym}\nShort Interest: {short_data.short_interest / short_data.shares_outstanding:.1%}\nVolume: {bars_df['v'].iloc[-1]:,} (3x+ avg) | Price: +{((price - bars_df['c'].iloc[0])/bars_df['c'].iloc[0]*100):.1f}%\nSqueeze Score: {score}/100\nBuy 2x {int(best.strike_price)}c @ ${oquote.ask:.2f}\nEntry: Now | Exit: 50% @ +80% | 50% @ +150% | Trail"
            await send_alert(os.getenv("TELEGRAM_TOKEN_SQUEEZE"), f"SQUEEZE LONG {sym}", body, link, confidence)
        except: pass