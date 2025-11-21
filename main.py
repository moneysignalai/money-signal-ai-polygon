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


app = FastAPI()

# How often to run a full scan cycle (seconds).
# More aggressive default: 30 seconds instead of 60.
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))

# Ordered list of all bots we want to run every cycle.
# (public_name, module_path, function_name)
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
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "bots": [name for name, _, _ in BOTS] + ["status_report"],
    }


async def run_all_once():
    """Run one full scan cycle.

    Steps:
      1) Import status_report so we can forward errors and log bot runs.
      2) Import every bot in BOTS and schedule its coroutine.
      3) Schedule status_report.run_status_report() as another coroutine.
      4) Await all tasks with asyncio.gather(return_exceptions=True).
      5) For each result:
           - If it's an Exception → record via status_report.record_bot_error + log_bot_run("error").
           - Else → log_bot_run("ok").
    """
    # 1) Try to import status_report helpers first
    record_error = None
    run_status = None
    log_bot_run = None

    try:
        status_mod = importlib.import_module("bots.status_report")
        record_error = getattr(status_mod, "record_bot_error", None)
        run_status = getattr(status_mod, "run_status_report", None)
        log_bot_run = getattr(status_mod, "log_bot_run", None)
    except Exception as e:
        print("[main] ERROR importing bots.status_report:", e)
        status_mod = None

    tasks = []
    names = []

    # 2) Import every bot and schedule its run_* coroutine
    for public_name, module_path, func_name in BOTS:
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name, None)
            if fn is None:
                raise AttributeError(f"{module_path}.{func_name} not found")

            # Log that we are scheduling this bot in this cycle
            print(f"[main] scheduling bot '{public_name}' ({module_path}.{func_name})")
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

    # 3) Add status_report as just another async task if available
    if run_status is not None:
        try:
            print("[main] scheduling bot 'status_report' (bots.status_report.run_status_report)")
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

    # 4) Run all bots concurrently, but capture exceptions as results
    print(f"[main] running {len(tasks)} bot tasks concurrently...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 5) Interpret results and forward to status_report
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            # Bot raised an exception during execution
            print(f"[ERROR] Bot {name} raised: {result}")
            if record_error:
                try:
                    record_error(name, result)
                except Exception as inner:
                    print("[main] ERROR while recording bot runtime error:", inner)
            if log_bot_run:
                try:
                    log_bot_run(name, "error")
                except Exception as inner:
                    print("[main] ERROR while logging bot run (error):", inner)
        else:
            # Successful completion (even if that bot just skipped due to time window / filters)
            print(f"[main] bot '{name}' completed cycle without crash")
            if log_bot_run:
                try:
                    log_bot_run(name, "ok")
                except Exception as inner:
                    print("[main] ERROR while logging bot run (ok):", inner)


async def scheduler_loop(interval_seconds: int = SCAN_INTERVAL_SECONDS):
    """Main scheduler loop.

    Repeatedly:
      • Logs the cycle number.
      • Calls run_all_once().
      • Sleeps for interval_seconds.
    Any unexpected exceptions at the scheduler level are logged but do NOT stop the loop.
    """
    cycle = 0
    print(
        f"[main] MoneySignalAI scheduler starting at {now_est_str()} "
        f"with interval={interval_seconds}s"
    )

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

        print(
            f"[main] cycle #{cycle} finished at {now_est_str()} — "
            f"sleeping {interval_seconds} seconds before next scan."
        )
        await asyncio.sleep(interval_seconds)


def _start_background_scheduler():
    """Starts the asyncio scheduler loop in a dedicated background thread.

    This allows FastAPI/uvicorn to serve HTTP requests while the scheduler runs
    in the background.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    """FastAPI startup hook.

    On app startup (Render boot/redeploy), spin up the background scheduler thread.
    """
    print(f"[main] startup_event fired at {now_est_str()}")
    print(f"[main] launching background scheduler thread (interval={SCAN_INTERVAL_SECONDS}s)")
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))