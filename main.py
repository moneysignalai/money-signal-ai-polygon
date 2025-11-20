import os
import threading
import asyncio
import importlib
from datetime import datetime

import pytz
import uvicorn
from fastapi import FastAPI

eastern = pytz.timezone("US/Eastern")


def now_est_str() -> str:
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


app = FastAPI(title="MoneySignalAI — Multi-Bot Suite")

# List of all bots: (public_name, module_path, function_name)
BOTS = [
    ("premarket", "bots.premarket", "run_premarket"),
    ("gap", "bots.gap", "run_gap"),
    ("orb", "bots.orb", "run_orb"),
    ("volume", "bots.volume", "run_volume"),
    ("cheap", "bots.cheap", "run_cheap"),
    ("unusual", "bots.unusual", "run_unusual"),
    ("squeeze", "bots.squeeze", "run_squeeze"),
    ("earnings", "bots.earnings", "run_earnings"),
    ("momentum_reversal", "bots.momentum_reversal", "run_momentum_reversal"),
    ("whales", "bots.whales", "run_whales"),
    ("trend_rider", "bots.trend_rider", "run_trend_rider"),
    ("swing_pullback", "bots.swing_pullback", "run_swing_pullback"),
    ("panic_flush", "bots.panic_flush", "run_panic_flush"),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar"),
    ("iv_crush", "bots.iv_crush", "run_iv_crush"),
]


@app.get("/")
def root():
    """Simple health endpoint for Render / browser checks."""
    return {
        "status": "LIVE",
        "timestamp": now_est_str(),
        "bots": [name for name, _, _ in BOTS] + ["status_report"],
    }


async def run_all_once():
    """
    One full scan cycle:
      - Dynamically import each bot module.
      - Schedule all found bot coroutines in parallel.
      - Also schedule status_report.run_status_report() if available.
      - Catch *all* exceptions and forward them to status_report.record_bot_error.
    """
    # Try to import status_report first so we can record errors there.
    status_mod = None
    record_error = None
    run_status = None

    try:
        status_mod = importlib.import_module("bots.status_report")
        record_error = getattr(status_mod, "record_bot_error", None)
        run_status = getattr(status_mod, "run_status_report", None)
    except Exception as e:
        print("[main] ERROR importing bots.status_report:", e)
        status_mod = None

    tasks = []
    names = []

    # Dynamically import and schedule each bot
    for public_name, module_path, func_name in BOTS:
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name, None)
            if fn is None:
                raise AttributeError(f"{module_path}.{func_name} not found")

            coro = fn()
            tasks.append(coro)
            names.append(public_name)

        except Exception as e:
            # Import/config error for this bot → log + report, but do NOT crash the app
            print(f"[main] ERROR importing/initializing bot {public_name} ({module_path}.{func_name}): {e}")
            if record_error:
                try:
                    record_error(public_name, e)
                except Exception as inner:
                    print("[main] ERROR while recording bot import error:", inner)

    # Add status_report as just another async task if available
    if run_status is not None:
        try:
            tasks.append(run_status())
            names.append("status_report")
        except Exception as e:
            print("[main] ERROR scheduling status_report:", e)
            if record_error:
                try:
                    record_error("status_report", e)
                except Exception as inner:
                    print("[main] ERROR while recording status_report scheduling error:", inner)

    if not tasks:
        print("[main] No bot tasks scheduled in this cycle.")
        return

    # Run all bots concurrently, but capture exceptions as results
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for name, result in zip(names, results):
        if isinstance(result, Exception):
            print(f"[ERROR] Bot {name} raised: {result}")
            if record_error:
                try:
                    record_error(name, result)
                except Exception as inner:
                    print("[main] ERROR while recording bot runtime error:", inner)


async def scheduler_loop(interval_seconds: int = 60):
    """
    Main scheduler loop:
      - Runs run_all_once() every interval_seconds.
      - Catches any unexpected error at the scheduler level so it never dies.
    """
    cycle = 0
    print(f"[main] MoneySignalAI scheduler starting at {now_est_str()}")

    while True:
        cycle += 1
        print(
            f"[main] SCANNING CYCLE #{cycle} — "
            "Premarket, Gap, ORB, Volume, Cheap, Unusual, Squeeze, Earnings, "
            "Momentum, Whales, TrendRider, Pullback, PanicFlush, DarkPool, IV Crush, Status"
        )
        try:
            await run_all_once()
        except Exception as e:
            # Last-resort catch — we log but keep the scheduler alive
            print("[main] FATAL error in run_all_once():", e)

        await asyncio.sleep(interval_seconds)


def _start_background_scheduler():
    """
    Starts the asyncio scheduler loop in a dedicated background thread so the
    FastAPI app (and uvicorn) can still serve HTTP requests.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    """
    On FastAPI startup (Render boot/redeploy), spin up the background scheduler thread.
    """
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))