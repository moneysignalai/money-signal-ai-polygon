import os
import threading
import time
import asyncio

import requests
from fastapi import FastAPI
import uvicorn

from bots.cheap import run_cheap
from bots.earnings import run_earnings
from bots.gap import run_gap
from bots.orb import run_orb
from bots.squeeze import run_squeeze
from bots.unusual import run_unusual
from bots.volume import run_volume
from bots.momentum_reversal import run_momentum_reversal
from bots.shared import (
    now_est,
    TELEGRAM_CHAT_ALL,
    TELEGRAM_TOKEN_STATUS,
    start_polygon_websocket,
)

app = FastAPI(title="MoneySignalAi — MEGA BOT (9 strategies)")


@app.get("/")
def root():
    """Health check endpoint for Render."""
    return {"status": "LIVE", "time": now_est()}


def send_status() -> None:
    """
    Send a periodic mega-bot status message to Telegram.

    Uses TELEGRAM_TOKEN_STATUS and TELEGRAM_CHAT_ALL from the environment.
    If not configured, this quietly does nothing.
    """
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL:
        print("STATUS: missing TELEGRAM_TOKEN_STATUS or TELEGRAM_CHAT_ALL")
        return

    message = f"""*MoneySignalAi — MEGA BOT STATUS*  
{now_est()}  

✅ Premarket scanner  
✅ ORB strategy  
✅ Short squeeze  
✅ Unusual option buyers  
✅ Cheap 0DTE + 3DTE  
✅ Gap up / Gap down  
✅ Earnings  
✅ Top daily volume  
✅ Momentum reversal  

Polygon WebSocket: connected  
Scanner: running every 30 seconds."""

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage"
    try:
        requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ALL,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        print("STATUS SENT")
    except Exception as e:
        print(f"STATUS SEND FAILED: {e}")


async def run_all_once() -> None:
    """
    Run one full scan of all bots in parallel.

    For now this just calls every bot once. Each bot should internally
    decide if it should do anything (e.g. time windows, filters).
    """
    tasks = [
        run_cheap(),
        run_earnings(),
        run_gap(),
        run_orb(),
        run_squeeze(),
        run_unusual(),
        run_volume(),
        run_momentum_reversal(),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    bot_names = [
        "cheap",
        "earnings",
        "gap",
        "orb",
        "squeeze",
        "unusual",
        "volume",
        "momentum",
    ]

    for name, result in zip(bot_names, results):
        if isinstance(result, Exception):
            print(f"[ERROR] Bot {name} raised: {result}")


def run_forever() -> None:
    """
    Background thread: create a single event loop and keep
    running all bots every 30 seconds.
    """
    print("MoneySignalAi MEGA BOT LIVE")
    start_polygon_websocket()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    cycle = 0
    while True:
        cycle += 1
        print(f"SCAN #{cycle} @ {now_est()}")
        print(
            "SCANNING: Premarket, ORB, Short Squeeze, "
            "Unusual, Cheap 0DTE/3DTE, Gaps, Earnings, Volume, Momentum"
        )

        # every ~2 hours (30s * 240)
        if cycle % 240 == 0:
            send_status()

        try:
            loop.run_until_complete(run_all_once())
        except Exception as e:
            print(f"[FATAL] run_all_once crashed: {e}")

        time.sleep(30)


@app.on_event("startup")
async def startup_event():
    """Start the scanner thread when FastAPI boots on Render."""
    threading.Thread(target=run_forever, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))