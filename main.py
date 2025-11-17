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
    return datetime.now(eastern).strftime("%I:%M %p")

app = FastAPI(title="MoneySignalAi — ELITE 8-BOT SUITE")

@app.get("/")
def root():
    return {"status": "LIVE — 8 BOTS ACTIVE", "time": now_est()}

# ——— ALL ENVIRONMENT VARIABLES (MUST be in main.py) ———
TELEGRAM_CHAT_ALL       = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL     = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN     = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW     = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP      = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB      = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE  = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL  = os.getenv("TELEGRAM_TOKEN_UNUSUAL")
TELEGRAM_TOKEN_MOMENTUM = os.getenv("TELEGRAM_TOKEN_MOMENTUM")   # 8th bot
TELEGRAM_TOKEN_STATUS   = os.getenv("TELEGRAM_TOKEN_STATUS")     # status bot

# Import bots
from bots.cheap           import run_cheap
from bots.earnings        import run_earnings
from bots.gap             import run_gap
from bots.orb             import run_orb
from bots.squeeze         import run_squeeze
from bots.unusual         import run_unusual
from bots.volume          import run_volume
from bots.momentum_reversal import run_momentum_reversal

from bots.shared import start_polygon_websocket

# ——— ALERT FUNCTION (uses all 8 tokens) ———
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = TELEGRAM_TOKEN_FLOW
    if "cheap" in bot_name.lower():     token = TELEGRAM_TOKEN_DEAL
    if "earn" in bot_name.lower():      token = TELEGRAM_TOKEN_EARN
    if "gap" in bot_name.lower():       token = TELEGRAM_TOKEN_GAP
    if "orb" in bot_name.lower():       token = TELEGRAM_TOKEN_ORB
    if "squeeze" in bot_name.lower():   token = TELEGRAM_TOKEN_SQUEEZE
    if "unusual" in bot_name.lower():   token = TELEGRAM_TOKEN_UNUSUAL
    if "momentum" in bot_name.lower():  token = TELEGRAM_TOKEN_MOMENTUM

    if not token or not TELEGRAM_CHAT_ALL: return

    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
        print(f"ALERT → {message}")
    except Exception as e:
        print(f"ALERT FAILED → {e}")

# ——— STATUS REPORT (uses dedicated token) ———
def send_status():
    token = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_FLOW
    if not token or not TELEGRAM_CHAT_ALL: return
    msg = f"""*MoneySignalAi — ELITE SUITE STATUS*  
{now_est()} EST  

8 bots live · Polygon connected · Scanner active  

Next wave: 9:30–10:30 AM → Gap + ORB + Cheap  
2:30–4:00 PM → Unusual + Squeeze + Volume + Momentum  

Ready for tomorrow’s massacre"""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# ——— RUN ALL 8 BOTS ———
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
    while True:
        cycle += 1
        now = now_est()
        print(f"SCAN #{cycle} @ {now}")
        if cycle % 60 == 0:  # every 30 min
            send_status()
        asyncio.new_event_loop().run_until_complete(run_all_once())
        time.sleep(30)

@app.on_event("startup")
async def startup():
    threading.Thread(target=run_forever, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))