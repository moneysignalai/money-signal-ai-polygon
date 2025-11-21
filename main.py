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

# ---------------- CONFIG ----------------
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
BOT_TIMEOUT_SECONDS = int(os.getenv("BOT_TIMEOUT_SECONDS", "40"))

# ---------------- ACTIVE BOTS ----------------
# This matches the NEW folder layout exactly.
BOTS = [
    ("premarket", "bots.premarket", "run_premarket"),

    ("openingrangebreakout", "bots.openingrangebreakout", "run_openingrangebreakout"),

    ("intraday_flow", "bots.intraday_flow", "run_intraday_flow"),

    ("trend_flow", "bots.trend_flow", "run_trend_flow"),

    ("equity_flow", "bots.equity_flow", "run_equity_flow"),

    ("options_flow", "bots.options_flow", "run_options_flow"),

    ("squeeze", "bots.squeeze", "run_squeeze"),

    ("earnings", "bots.earnings", "run_earnings"),

    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar"),
]


@app.get("/")
def root():
    return {
        "status": "LIVE",
        "timestamp": now_est_str(),
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "bot_timeout_seconds": BOT_TIMEOUT_SECONDS,
        "bots": [name for name, _, _ in BOTS] + ["status_report"],
    }


async def run_all_once():
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

    tasks = []
    names = []

    for public_name, module_path, func_name in BOTS:
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name, None)
            if fn is None:
                raise AttributeError(f"{module_path}.{func_name} not found")

            print(f"[main] scheduling bot '{public_name}'")

            maybe_coro = fn()
            if asyncio.iscoroutine(maybe_coro):
                task = asyncio.wait_for(maybe_coro, timeout=BOT_TIMEOUT_SECONDS)
            else:
                async def _run_sync(sync_fn=fn):
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, sync_fn)
                task = asyncio.wait_for(_run_sync(), timeout=BOT_TIMEOUT_SECONDS)

            tasks.append(task)
            names.append(public_name)

        except Exception as e:
            print(f"[main] ERROR initializing bot {public_name}: {e}")
            if record_error:
                try:
                    record_error(public_name, e)
                except:
                    pass

    # status_report as a bot
    if run_status is not None:
        try:
            status_coro = run_status()
            if asyncio.iscoroutine(status_coro):
                task = asyncio.wait_for(status_coro, timeout=BOT_TIMEOUT_SECONDS)
            else:
                async def _run_sync(sf=run_status):
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, sf)
                task = asyncio.wait_for(_run_sync(), timeout=BOT_TIMEOUT_SECONDS)

            tasks.append(task)
            names.append("status_report")
        except Exception as e:
            print("[main] ERROR scheduling status_report:", e)

    if not tasks:
        print("[main] No bot tasks scheduled.")
        return

    print(f"[main] running {len(tasks)} tasks...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # interpret results
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            print(f"[ERROR] {name}: {result}")
            if record_error:
                try:
                    record_error(name, result)
                except:
                    pass
            if log_bot_run:
                try:
                    log_bot_run(name, "error")
                except:
                    pass
        else:
            if log_bot_run:
                try:
                    log_bot_run(name, "ok")
                except:
                    pass


async def scheduler_loop(interval_seconds: int = SCAN_INTERVAL_SECONDS):
    cycle = 0
    print(
        f"[main] Scheduler started at {now_est_str()} "
        f"(interval={interval_seconds}s, timeout={BOT_TIMEOUT_SECONDS}s)"
    )

    while True:
        cycle += 1
        print(f"[main] SCANNING CYCLE #{cycle}")
        try:
            await run_all_once()
        except Exception as e:
            print("[main] FATAL:", e)

        print(f"[main] cycle #{cycle} done. Sleeping {interval_seconds}s.")
        await asyncio.sleep(interval_seconds)


def _start_background_scheduler():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    print(f"[main] startup_event @ {now_est_str()}")
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
