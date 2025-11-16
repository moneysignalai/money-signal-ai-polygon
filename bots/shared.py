# bots/shared.py
import telegram
from polygon import RESTClient
import os
from datetime import datetime, timedelta
import pandas as pd
from ta.momentum import RSIIndicator

# Polygon client
polygon_client = RESTClient(api_key=os.getenv("POLYGON_KEY"))

# Cache for top 500
HIGH_LIQUIDITY_UNIVERSE = None

async def get_top_500_universe():
    """Scan ALL US stocks → return top 500 by 30-day avg volume > 1M, price > $10"""
    global HIGH_LIQUIDITY_UNIVERSE
    if HIGH_LIQUIDITY_UNIVERSE is not None:
        return HIGH_LIQUIDITY_UNIVERSE

    # Get all US stocks
    tickers_resp = polygon_client.list_tickers(market="stocks", limit=1000)
    tickers = tickers_resp.results

    filtered = []
    for t in tickers:
        if t.market != "stocks" or t.type != "CS" or t.locale != "us":
            continue
        try:
            agg = polygon_client.get_aggs(t.ticker, 1, "day", limit=30)
            if not agg.results:
                continue
            df = pd.DataFrame(agg.results)
            avg_vol = df['v'].mean()
            last_price = df['c'].iloc[-1]
            if avg_vol > 1_000_000 and last_price > 10:
                filtered.append((t.ticker, avg_vol))
        except:
            continue

    filtered.sort(key=lambda x: x[1], reverse=True)
    HIGH_LIQUIDITY_UNIVERSE = [t[0] for t in filtered[:500]]
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
    gamma_ok = getattr(quote, 'gamma', 0) > 0.08
    vol_oi = quote.volume / max(getattr(quote, 'open_interest', 1), 1) > 1.2
    return delta_ok and gamma_ok and vol_oi

def get_confidence_score(vol_oi, gamma, iv, fvg_ok, mtf_ok):
    score = 0
    if vol_oi > 3: score += 30
    if gamma > 0.08: score += 25
    if iv < 0.3: score += 20
    if fvg_ok: score += 25
    if mtf_ok: score += 20
    return min(100, score)

def mtf_confirm(sym, direction):
    bars = polygon_client.get_aggs(sym, 15, "minute", limit=5)
    if not bars.results: return False
    df = pd.DataFrame(bars.results)
    trend = "UP" if df['c'].iloc[-1] > df['c'].iloc[-5] else "DOWN"
    return (direction == "LONG" and trend == "UP") or (direction == "SHORT" and trend == "DOWN")

async def send_alert(token, title, body, link=None, confidence=0):
    """Send to PERSONAL CHAT (TELEGRAM_CHAT_ALL) — no channels"""
    bot = telegram.Bot(token=token)
    msg = f"*{title}*\n{body}"
    if confidence:
        stars = "⭐" * (confidence // 20)
        msg += f"\n**Confidence: {confidence}/100 {stars}**"
    if link:
        msg += f"\n[TAP TO TRADE]({link})"
    await bot.send_message(
        chat_id=os.getenv("TELEGRAM_CHAT_ALL"),  # ← PERSONAL ID
        text=msg.strip(),
        parse_mode='Markdown',
        disable_web_page_preview=True
    )
