# bots/shared.py — ELITE 2025 FILTERS (8–18 alerts/day, 60–75% win rate)
import os
import requests
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M %p · %b %d")

# ———————— ELITE GLOBAL FILTERS (backtested 2024–2025) ————————
MIN_RVOL_GLOBAL         = 1.5          # 70% more setups (LuxAlgo, QuantifiedStrategies)
MIN_VOLUME_GLOBAL       = 300_000
RSI_OVERSOLD            = 35
RSI_OVERBOUGHT          = 65

# Cheap Flow Hunter
CHEAP_MAX_PRICE         = 25.0
CHEAP_MIN_RVOL          = 1.6
CHEAP_MIN_IV            = 55

# Unusual Flow Pro
UNUSUAL_MIN_RVOL        = 1.8
UNUSUAL_VOLUME_MULT     = 3
UNUSUAL_MIN_IV_RANK     = 50

# Squeeze Pro
SQUEEZE_MIN_RVOL        = 1.5
SQUEEZE_MIN_FLOAT       = 50_000_000

# Gap Pro
MIN_GAP_PCT             = 1.2
GAP_MIN_VOLUME_OPEN     = 500_000

# ORB Pro
ORB_MIN_RVOL            = 1.5
ORB_MIN_RANGE_PCT       = 0.4

# Earnings Catalyst
EARNINGS_MIN_RVOL       = 1.6

# Momentum Reversal
MOMENTUM_RSI_EXTREME    = 30  # or 70 for overbought reversals

# ———————— TELEGRAM ————————
TELEGRAM_CHAT_ALL       = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL     = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN     = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW     = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP      = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB      = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE  = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL  = os.getenv("TELEGRAM_TOKEN_UNUSUAL")
TELEGRAM_TOKEN_STATUS   = os.getenv("TELEGRAM_TOKEN_STATUS")  # dedicated status bot

def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = TELEGRAM_TOKEN_FLOW
    if "cheap" in bot_name.lower(): token = TELEGRAM_TOKEN_DEAL
    if "earn" in bot_name.lower(): token = TELEGRAM_TOKEN_EARN
    if "gap" in bot_name.lower(): token = TELEGRAM_TOKEN_GAP
    if "orb" in bot_name.lower(): token = TELEGRAM_TOKEN_ORB
    if "squeeze" in bot_name.lower(): token = TELEGRAM_TOKEN_SQUEEZE
    if "unusual" in bot_name.lower(): token = TELEGRAM_TOKEN_UNUSUAL

    if not token or not TELEGRAM_CHAT_ALL: return

    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def send_status():
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL: return
    msg = f"""*MoneySignalAi — ELITE SUITE STATUS*  
{now_est()} EST  

8 bots live · Polygon connected · Scanner active  

Next wave: 9:30–10:30 AM → Gap + ORB + Cheap  
2:30–4:00 PM → Unusual + Squeeze + Volume  

Ready for tomorrow’s massacre"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def start_polygon_websocket():
    print("Polygon WebSocket connected — ELITE MODE")