# main.py
from fastapi import FastAPI
from bots.orb import run_orb
from bots.earnings import run_earnings
from bots.cheap import run_cheap
from bots.volume import run_volume
from bots.unusual import run_unusual
from bots.squeeze import run_squeeze
from bots.gap import run_gap
import asyncio
from datetime import datetime
import os
from dotenv import load_dotenv
load_dotenv()

app = FastAPI()

async def run_all():
    while True:
        now = datetime.now()
        tasks = [run_orb(), run_earnings(), run_volume(), run_gap()]
        if 9 <= now.hour < 16 and now.minute % 15 == 0:
            tasks += [run_cheap(), run_unusual(), run_squeeze()]
        await asyncio.gather(*tasks)
        await asyncio.sleep(60)

@app.on_event("startup")
async def start():
    asyncio.create_task(run_all())

@app.get("/")
def home():
    return {
        "status": "MoneySignalAi 7 Bots Live — ULTIMATE EDITION",
        "user": "@HuskersTalk",
        "bots": ["ORB", "Earnings", "Cheap", "Volume", "Unusual", "Squeeze", "Gap"],
        "time": "2025-11-17 10:43 EST"
    }

# Test endpoint (remove after testing)
@app.get("/test")
async def test():
    from bots.shared import send_alert
    await send_alert(
        token=os.getenv("TELEGRAM_TOKEN_ORB"),
        title="TEST ALERT",
        body="Your @MoneySignalAi 7-bot suite is LIVE!\n\n6 bots running:\n• ORB\n• Earnings\n• Cheap\n• Volume\n• Unusual\n• Squeeze\n• Gap\n\nFirst real alert: 9:30 AM EST tomorrow.",
        link=None,
        confidence=100
    )
    return {"sent": True}