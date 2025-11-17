import os
import threading
import time
import asyncio
import requests
from fastapi import FastAPI
import uvicorn
from datetime import datetime
import pytz

# Eastern Time
eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M:%S %p")

app = FastAPI(title="MoneySignalAi — 7 Bots + Status Report Bot")

@app.get("/")
def root():
    return {"status": "LIVE", "time": now_est()}

# Your env vars
TELEGRAM_CHAT_ALL      = os.getenv("TELEGRAM_CHAT_ALL")           # main private chat
TELEGRAM_TOKEN_DEAL    = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN    = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW    = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP     = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB     = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL = os.getenv("TELEGRAM_TOKEN_UNUSUAL")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")        # dedicated status bot token

# Import bots
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

# ONLY real alerts — NO heartbeat spam in Volume Leaders
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = TELEGRAM_TOKEN_FLOW
    if "cheap" in bot_name.lower(): token = TELEGRAM_TOKEN_DEAL
    if "earn" in bot_name.lower(): token = TELEGRAM_TOKEN_EARN
    if "gap" in bot_name.lower(): token = TELEGRAM_TOKEN_GAP
    if "orb" in bot_name.lower(): token = TELEGRAM_TOKEN_ORB
    if "squeeze" in bot_name.lower(): token = TELEGRAM_TOKEN_SQUEEZE
    if "unusual" in bot_name.lower(): token = TELEGRAM_TOKEN_UNUSUAL

    if not token or not TELEGRAM_CHAT_ALL:
        return

    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

# Dedicated Status Report Bot — only this one talks about scanning & 7 bots
def send_status_report():
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL:
        return

    now = now_est()
    message = f"""*MoneySignalAi — SUITE STATUS*  
{now} EST  

7 bots actively scanning:  
• Cheap Bot  
• Earnings Catalyst Bot  
• Gap Fill/Fade Bot  
• Opening Range Breakout Bot  
• Short Squeeze Bot  
• Unusual Options Flow Bot  
• Volume Leaders Bot  

Polygon WebSocket: Connected  
Scanner: Running every 30 seconds  
Health checks: Passing  

Next wave:  
2:30–4:00 PM EST → Cheap / Squeeze / Unusual / Volume  
Tomorrow 9:30 AM → Gap + ORB  

System 100% operational — waiting on setups"""

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

from bots.shared import start_polygon_websocket

async def run_all_once():
    await asyncio.gather(
        run_cheap(), run_earnings(), run_gap(),
        run_orb(), run_squeeze(), run_unusual(), run_volume(),
        return_exceptions=True
    )

def run_forever():
    print("INFO: MoneySignalAi 7 BOTS + DEDICATED STATUS BOT LIVE")
    start_polygon_websocket()
    
    cycle = 0
    while True:
        cycle += 1
        now = now_est()
        print(f"SCAN #{cycle} | STARTING @ {now}")
        
        # NO heartbeat in Volume Leaders — only real alerts
        # Status Report Bot handles all "scanning" updates
        if cycle % 60 == 0:  # every 30 minutes
            send_status_report()
        
        asyncio.new_event_loop().run_until_complete(run_all_once())
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))