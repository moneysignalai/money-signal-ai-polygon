import os
import threading
import time
import asyncio
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def root():
    return {"status": "LIVE — 7 BOTS ACTIVE", "time": time.strftime("%I:%M %p")}

# Your existing 7 trading tokens + chat
TELEGRAM_CHAT_ALL      = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL    = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN    = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW    = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP     = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB     = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL = os.getenv("TELEGRAM_TOKEN_UNUSUAL")

from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

# NEW — dedicated status bot
from bots.status_report import run_status_report

def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = TELEGRAM_TOKEN_FLOW
    if "cheap" in bot_name.lower(): token = TELEGRAM_TOKEN_DEAL
    if "earn" in bot_name.lower(): token = TELEGRAM_TOKEN_EARN
    if "gap" in bot_name.lower(): token = TELEGRAM_TOKEN_GAP
    if "orb" in bot_name.lower(): token = TELEGRAM_TOKEN_ORB
    if "squeeze" in bot_name.lower(): token = TELEGRAM_TOKEN_SQUEEZE
    if "unusual" in bot_name.lower(): token = TELEGRAM_TOKEN_UNUSUAL

    if not token or not TELEGRAM_CHAT_ALL:
        print(f"ALERT (no creds): {bot_name} → {ticker} {extra}")
        return

    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        import requests
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ALL, "text": message, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass  # silent fail — we don’t want status spam in trading channels

from bots.shared import start_polygon_websocket

async def run_all_once():
    await asyncio.gather(
        run_cheap(), run_earnings(), run_gap(),
        run_orb(), run_squeeze(), run_unusual(), run_volume(),
        return_exceptions=True
    )

def run_forever():
    print("INFO: MoneySignalAi 7-BOT SUITE + DEDICATED STATUS BOT LIVE")
    start_polygon_websocket()
    
    cycle = 0
    while True:
        cycle += 1
        now = time.strftime("%I:%M:%S %p")
        print(f"SCAN #{cycle} | STARTING @ {now}")
        # Heartbeat only in trading channel
        send_alert("Scanner", "Now Scanning", 0, 0, f"Cycle #{cycle} • {now} EST")
        
        # Dedicated status report — completely separate
        if cycle % 120 == 0:  # once per hour
            asyncio.new_event_loop().run_until_complete(run_status_report())
        
        asyncio.new_event_loop().run_until_complete(run_all_once())
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))