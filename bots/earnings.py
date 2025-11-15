# bots/earnings.py
from .shared import *
import yfinance as yf

async def run_earnings():
    now = datetime.now()
    if now.hour < 7 or now.hour >= 18 or now.weekday() >= 5: return

    universe = await get_top_500_universe()
    for sym in universe[:50]:
        try:
            ticker = yf.Ticker(sym)
            info = ticker.info
            if 'earningsDate' not in info: continue

            if now.hour in [7, 8]:
                news_resp = polygon_client.list_ticker_news(sym, published_utc_gte=(now - timedelta(days=1)).isoformat(), limit=5)
                news = news_resp.results
                sentiment, opt_type = 'neutral', None
                for n in news:
                    s, t = get_news_sentiment(n.title, n.description)
                    if t: sentiment, opt_type = s, t
                if opt_type is None: continue

                exp = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp, contract_type=opt_type)
                contracts = contracts_resp.results
                price = polygon_client.get_last_trade(sym).price
                atm = [c for c in contracts if abs(c.strike_price - price) < 3]
                if not atm: continue
                best = max(atm, key=lambda x: x.volume)
                oquote = get_greeks(sym, exp, best.strike_price, opt_type)
                if not oquote or not is_edge_option(oquote): continue

                link = build_rh_link(sym, exp, best.strike_price, opt_type)
                body = f"PRE-EARNINGS {opt_type.upper()} | {now.strftime('%H:%M')} EST\nWhisper: {sentiment.upper()} News\nBuy 2x {int(best.strike_price)}{opt_type[0]} @ ${oquote.ask:.2f}\nIV: {oquote.implied_volatility:.0%} | Delta: {oquote.delta:.2f}\nEntry: Gap Play | Exit: 50% @ +100%"
                await send_alert(os.getenv("TELEGRAM_TOKEN_EARN"), f"PRE {sym} {opt_type.upper()}", body, link)
        except: pass
