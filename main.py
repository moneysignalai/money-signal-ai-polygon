import os
import threading
import time
import asyncio
from datetime import datetime
import pytz

import uvicorn
from fastapi import FastAPI

# --------- BOT IMPORTS ---------
# Core original bots (9-in-1)
from bots.premarket import run_premarket           # make sure bots/premarket.py exists
from bots.gap import run_gap
from bots.orb import run_orb
from bots.volume import run_volume
from bots.cheap import run_cheap
from bots.unusual import run_unusual
from bots.squeeze import run_squeeze
from bots.earnings import run_earnings
from bots.momentum_reversal import run_momentum_reversal

# Status / heartbeat bot (now also exposes error buffer API)
from bots.status_report import run_status_report, record_bot_error

# New advanced bots
from bots.whales import run_whales
from bots.trend_rider import run_trend_rider
from bots.swing_pullback import run_swing_pullback
from bots.panic_flush import run_panic_flush
from bots.dark_pool_radar import run_dark_pool_radar
from bots.iv_crush import run_iv_crush


eastern = pytz.timezone("US/Eastern")


def now_est_str() -> str:
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


app = FastAPI(title="MoneySignalAi — Multi-Bot Suite")


@app.get("/")
def root():
    """Simple health endpoint for Render / browser checks."""
    return {
        "status": "LIVE",
        "timestamp": now_est_str(),
        "bots": [
            "premarket",
            "gap",
            "orb",
            "volume",
            "cheap",
            "unusual",
            "squeeze",
            "earnings",
            "momentum_reversal",
            "whales",
            "trend_rider",
            "swing_pullback",
            "panic_flush",
            "dark_pool_radar",
            "iv_crush",
        ],
    }


async def run_all_once():
    """
    Kick off all bots once.

    IMPORTANT:
    - Each bot has its own time-window checks inside the file.
    - This function just calls everything; if it's outside a bot's window,
      that bot prints "Outside window; skipping" and returns quickly.
    """
    tasks = [
        # Core 9
        run_premarket(),
        run_gap(),
        run_orb(),
        run_volume(),
        run_cheap(),
        run_unusual(),
        run_squeeze(),
        run_earnings(),
        run_momentum_reversal(),

        # New bots
        run_whales(),
        run_trend_rider(),
        run_swing_pullback(),
        run_panic_flush(),
        run_dark_pool_radar(),
        run_iv_crush(),

        # Status / heartbeat (sends only at specific times)
        run_status_report(),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Log any exceptions instead of crashing the loop
    bot_names = [
        "premarket",
        "gap",
        "orb",
        "volume",
        "cheap",
        "unusual",
        "squeeze",
        "earnings",
        "momentum_reversal",
        "whales",
        "trend_rider",
        "swing_pullback",
        "panic_flush",
        "dark_pool_radar",
        "iv_crush",
        "status_report",
    ]

    for name, result in zip(bot_names, results):
        if isinstance(result, Exception):
            # Log to console for raw debugging
            print(f"[ERROR] Bot {name} raised: {result}")
            # Also push into the status_report error buffer
            record_bot_error(name, result)


def run_forever():
    """
    Background loop: runs run_all_once() every 60 seconds.

    - All time filters are implemented inside each bot.
    - If the market is closed, most bots just return immediately.
    """
    cycle = 0
    print(f"[main] MoneySignalAi multi-bot loop starting at {now_est_str()}")
    while True:
        cycle += 1
        print(
            f"SCANNING CYCLE #{cycle} — "
            "Premarket, Gap, ORB, Volume, Cheap, Unusual, Squeeze, Earnings, "
            "Momentum, Whales, TrendRider, Pullback, PanicFlush, DarkPool, IV Crush"
        )

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_all_once())
        finally:
            loop.close()

        # Adjust this if you want more or less frequency
        time.sleep(60)


@app.on_event("startup")
async def startup_event():
    """
    When FastAPI starts (Render boot / redeploy), start the scanner loop
    in a background thread so the HTTP server stays responsive.
    """
    threading.Thread(target=run_forever, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))