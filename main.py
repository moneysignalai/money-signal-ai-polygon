# main.py
import os
import threading
import time
import asyncio
import requests
from fastapi import FastAPI
from datetime import datetime
import pytz

eastern = pytz.timezone('US/Eastern')
def now_est():
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d")

app = FastAPI(title="MoneySignalAi — 9-BOT SUITE")

from bots.shared import send_alert, send_status, start_polygon_websocket
from bots.cheap import run_cheap
from bots.orb import run_orb
from bots.gap import run_gap
from bots.volume import run_volume
from bots.unusual import run_unusual
from bots.squeeze import run_squeeze
from bots.premarket import run_premarket
from bots.earnings import run_earnings
from bots.momentum_reversal import run_momentum_reversal

async def run_all_once():
    await asyncio.gather(
        run_cheap(), run_orb(), run_gap(), run_volume(),
        run_unusual(), run_squeeze(), run_premarket(),
        run_earnings(), run_momentum_reversal(),
        return_exceptions=True
    )

def run_forever():
    print("MoneySignalAi 9-BOT SUITE STARTED")
    start_polygon_websocket()
    cycle = 0
    while True:
        cycle += 1
        print(f"SCAN #{cycle} @ {now_est()}")
        asyncio.new_event_loop().run_until_complete(run_all_once())
        if cycle % 240 == 0:  # every 2 hours
            send_status()
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()

@app.get("/")
def root():
    return {"status": "LIVE — 9 BOTS SCANNING TOP VOLUME", "time": now_est()}