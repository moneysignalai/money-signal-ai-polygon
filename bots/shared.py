# bots/shared.py — FINAL: ONE CHAT, LOUD & CLEAN
import os
import requests
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M %p · %b %d")

TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN_DEAL") or os.getenv("TELEGRAM_TOKEN_FLOW")  # one token for all

async def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = "", link: str = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ALL:
        print(f"NO TELEGRAM → {ticker}")
        return

    labels = {
        "cheap":"CHEAP 0DTE HUNTER","orb":"OPENING RANGE BREAKOUT","gap":"GAP PRO",
        "volume":"VOLUME LEADERS","unusual":"UNUSUAL FLOW","squeeze":"SQUEEZE PRO",
        "earnings":"EARNINGS CATALYST","momentum":"MOMENTUM REVERSAL","premarket":"PRE-MARKET RUNNER"
    }
    label = labels.get(bot_name.lower(), bot_name.upper())

    msg = f"*{label}*\n**{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x\n{extra.strip()}\n\n*{now_est()}*"
    if link: msg += f"\n[Robinhood]({link})"

    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})
        print(f"ALERT → {label}: {ticker}")
    except: pass

def start_polygon_websocket():
    print("Polygon LIVE — 9-BOT SUITE ACTIVE")
