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
from bots.premarket import run_premarket
from bots.shared import (
    now_est,
    TELEGRAM_CHAT_ALL,
    TELEGRAM_TOKEN_STATUS,
    start_polygon_websocket,
)

app = FastAPI(title="MoneySignalAi — MEGA BOT (9 strategies + status)")


@app.get("/")
def root():
    """Health check endpoint for Render."""
    return {"status": "LIVE", "time": now_est()}


def send_status() -> None:
    """
    Status-report bot.

    Uses:
      - TELEGRAM_TOKEN_STATUS (status bot token)
      - TELEGRAM_CHAT_ALL    (where to send the message)

    Sends a summary that the scanners + loop are running.
    """
    if not TELEGRAM_CHAT_ALL:
        print("STATUS: TELEGRAM_CHAT_ALL not set")
        return

    token = TELEGRAM_TOKEN_STATUS
    if not token:
        print("STATUS: TELEGRAM_TOKEN_STATUS not set (skipping status send)")
        return

    message = f"""*MoneySignalAi — MEGA BOT STATUS*  
{now_est()}

✅ Premarket scanner  
✅ Top Volume scanner  
✅ ORB strategy  
✅ Short Squeeze scanner  
✅ Unusual Option Buyers  
✅ Cheap 0DTE / 3DTE scanner  
✅ Gap Up / Gap Down scanner  
✅ Earnings Movers scanner  
✅ Momentum Reversal scanner  

Polygon: REST scanners active  
Loop: running every 30 seconds on Render.
"""

    url = f"https://api.telegram.org/bot{token}/sendMessage"
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

    Each bot is async, and will decide internally whether it has any alerts.
    """
    tasks = [
        run_premarket(),          # NEW: premarket snapshot bot
        run_volume(),             # Top daily volume / intraday
        run_gap(),                # Gap up / gap down
        run_orb(),                # Opening range breakout
        run_squeeze(),            # Short-squeeze style
        run_unusual(),            # Unusual options flow
        run_cheap(),              # Cheap 0DTE/3DTE
        run_earnings(),           # Earnings movers
        run_momentum_reversal(),  # Intraday momentum reversal
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    bot_names = [
        "premarket",
        "volume",
        "gap",
        "orb",
        "squeeze",
        "unusual",
        "cheap",
        "earnings",
        "momentum_reversal",
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
            "SCANNING: Premarket, Volume, Gaps, ORB, Squeeze, "
            "Unusual, Cheap 0DTE/3DTE, Earnings, Momentum Reversal"
        )

        # Every ~2 hours: 30 seconds * 240 cycles
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