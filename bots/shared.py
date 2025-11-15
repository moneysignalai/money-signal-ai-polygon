# bots/shared.py
import telegram
from polygon import RESTClient
import os
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
from ta.momentum import RSIIndicator

polygon_client = RESTClient(api_key=os.getenv("POLYGON_KEY"))

HIGH_LIQUIDITY_UNIVERSE = None

async def get_top_500_universe():
    global HIGH_LIQUIDITY_UNIVERSE
    if HIGH_LIQUIDITY_UNIVERSE is not None:
        return HIGH_LIQUIDITY_UNIVERSE
    tickers = yf.Tickers('SPY QQQ IWM NVDA TSLA AAPL AMD META MSFT GOOGL AMZN NFLX ADBE ORCL CRM').tickers
    volumes = [t.info.get('averageVolume', 0) for t in tickers.values()]
    top = sorted(zip(tickers.keys(), volumes), key=lambda x: x[1], reverse=True)[:500]
    HIGH_LIQUIDITY_UNIVERSE = [s[0] for s in top]
    return HIGH_LIQUIDITY_UNIVERSE

def build_rh_link(sym, exp, strike, type_="call"):
    return f"robinhood://option/{sym}/{exp}/{strike}/{type_}"

def get_greeks(sym, exp, strike, type_="call"):
    try:
        contract = f"O:{sym}{exp.replace('-','')}{type_[0].upper()}{int(strike*1000):08d}"
        quote = polygon_client.get_option_quote(contract)
        return quote
    except:
        return None

def is_edge_option(quote):
    if not quote or quote.ask > 10: return False
    if quote.implied_volatility > 0.6: return False
    delta_ok = (0.4 <= abs(getattr(quote, 'delta', 0)) <= 0.7)
    gamma_ok = getattr(quote, 'gamma', 0) > 0.04
    vol_oi = quote.volume / max(getattr(quote, 'open_interest', 1), 1) > 1.2
    return delta_ok and gamma_ok and vol_oi

async def send_alert(token, title, body, link=None):
    bot = telegram.Bot(token=token)
    msg = f"*{title}*\n{body}"
    if link:
        msg += f"\n[TAP TO TRADE]({link})"
    await bot.send_message(
        chat_id=os.getenv(f"TELEGRAM_CHAT_{token[-6:]}", token),
        text=msg.strip(),
        parse_mode='Markdown',
        disable_web_page_preview=True
    )
