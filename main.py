import os
import threading
import time
import asyncio
import requests
from fastapi import FastAPI
import uvicorn
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M:%S %p")

app = FastAPI()

@app.get("/")
def root():
    return {"status": "LIVE — ELITE 8-BOT SUITE", "time": now_est()}

from bots.shared import send_alert, send_status, start_polygon_websocket
from bots.cheap import run_cheap
from bots.earnings import run_earnings
from bots.gap import run_gap
from bots.orb import run_orb
from bots.squeeze import run_squeeze
from bots.unusual import run_unusual
from bots.volume import run_volume
from bots.momentum_reversal import run_momentum_reversal  # NEW 8TH BOT

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