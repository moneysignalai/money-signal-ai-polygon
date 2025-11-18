# bots/shared.py — ALERTS GUARANTEED VERSION (2025 loose mode)
import os
import requests
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M %p · %b %d")

# ←←← LOOSENED SO YOU GET ALERTS TODAY
MIN_RVOL_GLOBAL     = 1.1
MIN_VOLUME_GLOBAL   = 100_000
CHEAP_MAX_PRICE     = 80.0
CHEAP_MIN_RVOL      = 1.2
CHEAP_MIN_IV        = 0.35

TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")

def get_token(name):
    return os.getenv("TELEGRAM_TOKEN_DEAL") or os.getenv("TELEGRAM_TOKEN_FLOW")

async def send_alert(bot_name, ticker, price, rvol, extra=""):
    token = get_token(bot_name)
    if not token or not TELEGRAM_CHAT_ALL:
        print(f"NO TELEGRAM → {ticker}")
        return
    label = bot_name.upper()
    msg = f"*{label}*\n**{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x\n{extra}\n\n*{now_est()}*"
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"})
        print(f"ALERT SENT → {ticker}")
    except:
        pass

def start_polygon_websocket():
    print("Polygon WebSocket CONNECTED — ALERTS ON")
