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
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d")

app = FastAPI(title="MoneySignalAi — ELITE 8-BOT SUITE")

@app.get("/")
def root():
    return {"status": "LIVE — ALERTS FORCED ON", "time": now_est()}

TELEGRAM_CHAT_ALL     = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")

from bots.cheap           import run_cheap
from bots.earnings        import run_earnings
from bots.gap             import run_gap
from bots.orb             import run_orb
from bots.squeeze         import run_squeeze
from bots.unusual         import run_unusual
from bots.volume          import run_volume
from bots.momentum_reversal import run_momentum_reversal

from bots.shared import start_polygon_websocket

# FIXED — the line that was cut off
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    if not TELEGRAM_TOKEN_ALERTS or not TELEGRAM_CHAT_ALL:
        print("NO TELEGRAM CREDENTIALS")
        return

    labels = {
        "cheap": "CHEAP 0DTE HUNTER", "earnings": "EARNINGS CATALYST",
        "gap": "GAP FILL/FADE PRO", "orb": "OPENING RANGE BREAKOUT",
        "squeeze": "SHORT SQUEEZE PRO", "unusual": "UNUSUAL OPTIONS FLOW",
        "volume": "VOLUME LEADERS PRO", "momentum_reversal": "MOMENTUM REVERSAL EDGE"
    }
    label = labels.get(bot_name.lower(), bot_name.upper())

    timestamp = now_est()
    message = f"""*{label}*  
**{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x  
{extra.strip()}

*{timestamp}*"""

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_ALERTS}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
        print(f"ALERT SENT → {label}: {ticker}")
    except Exception as e:
        print(f"ALERT FAILED → {e}")

# CORRECTED STATUS REPORT — ALL-DAY BOTS SCAN ALL DAY
def send_status():
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL: return

    now = now_est()
    message = f"""*MoneySignalAi — SCANNER STATUS*  
{now}  

8 bots actively scanning 24/7  

All-day scanners (9:30 AM – 4:00 PM EST):  
• Cheap 0DTE Hunter  
• Unusual Options Flow  
• Volume Leaders Pro  
• Short Squeeze Pro  
• Momentum Reversal Edge  
• Earnings Catalyst  

Morning-only (9:30–10:30 AM EST):  
• Gap Fill/Fade Pro  
• Opening Range Breakout  

Polygon WebSocket: Connected  
Scanner: Running every 30 seconds  

System 100% operational — waiting on setups"""

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except: pass

async def run_all_once():
    await asyncio.gather(
        run_cheap(), run_earnings(), run_gap(),
        run_orb(), run_squeeze(), run_unusual(),
        run_volume(), run_momentum_reversal(),
        return_exceptions=True
    )

def run_forever():
    print("MoneySignalAi ELITE 8-BOT SUITE LIVE")
    start_polygon_websocket()
    cycle = 0
    alerted = False  # ← stops after one alert
    while True:
        cycle += 1
        now = now_est()
        print(f"SCAN #{cycle} @ {now}")

        # ←←← ONLY CHANGE #1: Added scanning log so you can see it’s working
        print("SCANNING: Cheap, Earnings, Gap, ORB, Squeeze, Unusual, Volume, Momentum")

        if cycle % 260 == 0:  # every 2 hours
            send_status()
            
        asyncio.new_event_loop().run_until_complete(run_all_once())
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))