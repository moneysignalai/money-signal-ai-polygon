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

# How often to run a full scan cycle (seconds).
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))

# Per-bot hard timeout in seconds so a slow request can't block the whole cycle.
BOT_TIMEOUT_SECONDS = int(os.getenv("BOT_TIMEOUT_SECONDS", "40"))

# Ordered list of all bots we want to run every cycle.
# (public_name, module_path, function_name)
BOTS = [
    ("premarket", "bots.premarket", "run_premarket"),
    ("equity_flow", "bots.equity_flow", "run_equity_flow"),
    ("intraday_flow", "bots.intraday_flow", "run_intraday_flow"),
    ("opening_range_breakout", "bots.openingrangebreakout", "run_opening_range_breakout"),
    ("options_flow", "bots.options_flow", "run_options_flow"),
    ("squeeze", "bots.squeeze", "run_squeeze"),
    ("earnings", "bots.earnings", "run_earnings"),
    ("trend_flow", "bots.trend_flow", "run_trend_flow"),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar"),
]


@app.get("/")
def root():
    """Simple health endpoint for Render / browser checks."""
    return {
        "status": "LIVE",
        "timestamp": now_est_str(),
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "bot_timeout_seconds": BOT_TIMEOUT_SECONDS,
        "bots": [name for name, _, _ in BOTS] + ["status_report"],
    }


async def run_all_once():
    """
    Run one full scan cycle.

    Steps:
      1) Import status_report so we can forward errors and log bot runs.
      2) Import every bot in BOTS and schedule its coroutine (wrapped with timeout).
      3) Schedule status_report.run_status_report() as another coroutine if available.
      4) Await all tasks with asyncio.gather(return_exceptions=True).
      5) For each result, forward status to status_report.
    """
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

    # 2) Import every bot and schedule its run_* coroutine (with timeout wrapper)
    for public_name, module_path, func_name in BOTS:
        try:
            mod = importlib.import_module(module_path)
            fn = getattr(mod, func_name, None)
            if fn is None:
                raise AttributeError(f"{module_path}.{func_name} not found")

            print(f"[main] scheduling bot '{public_name}' ({module_path}.{func_name})")

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
            print(f"[main] ERROR importing/initializing bot {public_name} ({module_path}.{func_name}): {e}")
            if record_error:
                try:
                    record_error(public_name, e)
                except Exception as inner:
                    print("[main] ERROR while recording bot import error:", inner)

    # 3) Add status_report as another async task if available
    if run_status is not None:
        try:
            print("[main] scheduling bot 'status_report' (bots.status_report.run_status_report)")
            status_coro = run_status()
            if asyncio.iscoroutine(status_coro):
                status_task = asyncio.wait_for(status_coro, timeout=BOT_TIMEOUT_SECONDS)
            else:
                async def _run_status_sync(sf=run_status):
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(None, sf)

                status_task = asyncio.wait_for(_run_status_sync(), timeout=BOT_TIMEOUT_SECONDS)

            tasks.append(status_task)
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

    # 4) Run all bots concurrently
    print(f"[main] running {len(tasks)} bot tasks concurrently...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 5) Interpret results and forward to status_report
    for name, result in zip(names, results):
        if isinstance(result, Exception):
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
            print(f"[main] bot '{name}' completed cycle without crash")
            if log_bot_run:
                try:
                    log_bot_run(name, "ok")
                except Exception as inner:
                    print("[main] ERROR while logging bot run (ok):", inner)


async def scheduler_loop(interval_seconds: int = SCAN_INTERVAL_SECONDS):
    """Main scheduler loop."""
    cycle = 0
    print(
        f"[main] MoneySignalAI scheduler starting at {now_est_str()} "
        f"with interval={interval_seconds}s, bot_timeout={BOT_TIMEOUT_SECONDS}s"
    )

    while True:
        cycle += 1
        print(
            f"[main] SCANNING CYCLE #{cycle} — "
            "Premarket, EquityFlow, IntradayFlow, OpeningRangeBreakout, "
            "OptionsFlow, Squeeze, Earnings, TrendFlow, DarkPool, Status"
        )
        try:
            await run_all_once()
        except Exception as e:
            print("[main] FATAL error in run_all_once():", e)

        print(
            f"[main] cycle #{cycle} finished at {now_est_str()} — "
            f"sleeping {interval_seconds} seconds before next scan."
        )
        await asyncio.sleep(interval_seconds)


def _start_background_scheduler():
    """Starts the asyncio scheduler loop in a background thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    """FastAPI startup hook."""
    print(f"[main] startup_event fired at {now_est_str()}")
    print(
        f"[main] launching background scheduler thread "
        f"(interval={SCAN_INTERVAL_SECONDS}s, bot_timeout={BOT_TIMEOUT_SECONDS}s)"
    )
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
