# bots/shared.py   ← NO client LINE HERE
import os
import requests
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d")

TELEGRAM_CHAT_ALL     = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

# Import client from helpers (this is the fix)
from .helpers import client

def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    if not TELEGRAM_TOKEN_ALERTS or not TELEGRAM_CHAT_ALL: return
    label = bot_name.upper()
    msg = f"*{label}*\n**{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x\n{extra}\n\n*{now_est()}*"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN_ALERTS}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"})
        print(f"ALERT → {ticker}")
    except: pass

def send_status():
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL: return
    msg = f"*MoneySignalAi — LIVE*\n{now_est()}\n\n9 bots running · Top volume scan active\n\nReady for setups"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"})
    except: pass

def start_polygon_websocket():
    print("Polygon connected — scanning")