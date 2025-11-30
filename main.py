import os
import threading
import asyncio
import importlib
from datetime import datetime

import pytz
import uvicorn
from fastapi import FastAPI

# ----------------- Time helpers -----------------

eastern = pytz.timezone("US/Eastern")


def now_est_str() -> str:
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ----------------- Global config -----------------

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))
BOT_TIMEOUT_SECONDS = int(os.getenv("BOT_TIMEOUT_SECONDS", "40"))

# Ordered list of all bots we want to run every cycle.
# (public_name, module_path, function_name)
BOTS = [
    ("premarket", "bots.premarket", "run_premarket"),
    ("equity_flow", "bots.equity_flow", "run_equity_flow"),
    ("intraday_flow", "bots.intraday_flow", "run_intraday_flow"),
    ("rsi_signals", "bots.rsi_signals", "run_rsi_signals"),
    ("opening_range_breakout", "bots.openingrangebreakout", "run_opening_range_breakout"),
    ("options_flow", "bots.options_flow", "run_options_flow"),
    ("options_indicator", "bots.options_indicator", "run_options_indicator"),
    ("squeeze", "bots.squeeze", "run_squeeze"),
    ("earnings", "bots.earnings", "run_earnings"),
    ("trend_flow", "bots.trend_flow", "run_trend_flow"),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar"),
    ("daily_ideas", "bots.daily_ideas", "run_daily_ideas"),
]

# ----------------- FastAPI app -----------------

app = FastAPI(title="MoneySignalAI", version="1.0.0")


@app.get("/")
async def root():
    """
    Basic status endpoint.
    """
    return {
        "status": "ok",
        "now_est": now_est_str(),
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "bot_timeout_seconds": BOT_TIMEOUT_SECONDS,
        "bots": [b[0] for b in BOTS],
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "now_est": now_est_str()}


# ----------------- Scheduler logic -----------------


async def _run_single_bot(public_name: str, module_path: str, func_name: str, record_error):
    """
    Import and run a single bot with a per-bot timeout.
    """
    try:
        module = importlib.import_module(module_path)
        func = getattr(module, func_name, None)
        if func is None:
            raise AttributeError(f"{module_path} has no attribute {func_name}")

        # If the bot function is async, await it; otherwise run in thread pool.
        if asyncio.iscoroutinefunction(func):
            await asyncio.wait_for(func(), timeout=BOT_TIMEOUT_SECONDS)
        else:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.run_in_executor(None, func), timeout=BOT_TIMEOUT_SECONDS)

    except Exception as e:
        print(f"[main] ERROR running bot {public_name} ({module_path}.{func_name}): {e}")
        if record_error is not None:
            try:
                record_error(public_name, e)
            except Exception as inner:
                print("[main] ERROR while recording bot error:", inner)


async def scheduler_loop(interval_seconds: int = SCAN_INTERVAL_SECONDS):
    """
    Main async loop that runs all bots in parallel every `interval_seconds`.
    """
    print(f"[main] scheduler_loop starting with interval={interval_seconds}s, bot_timeout={BOT_TIMEOUT_SECONDS}s")

    # Lazy import status_report helpers so we can log errors & send heartbeat
    try:
        from bots.status_report import run_status, record_error
    except Exception as e:
        print(f"[main] WARNING: could not import bots.status_report: {e}")
        run_status = None  # type: ignore
        record_error = None  # type: ignore

    while True:
        start_ts = datetime.now(eastern)
        print(f"[main] scheduler cycle starting at {start_ts.strftime('%H:%M:%S')} ET")

        tasks = []

        # 1) schedule all trading bots
        for public_name, module_path, func_name in BOTS:
            task = asyncio.create_task(_run_single_bot(public_name, module_path, func_name, record_error))
            tasks.append(task)

        # 2) schedule status_report.run_status, if available
        if run_status is not None:
            try:
                tasks.append(asyncio.create_task(run_status()))
            except Exception as e:
                print(f"[main] ERROR scheduling run_status: {e}")

        # 3) wait for all tasks to complete (errors are logged inside the tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        end_ts = datetime.now(eastern)
        elapsed = (end_ts - start_ts).total_seconds()
        print(f"[main] scheduler cycle finished in {elapsed:.2f}s; sleeping {interval_seconds}s")

        await asyncio.sleep(interval_seconds)


def _start_background_scheduler():
    """
    Run the async scheduler loop in a dedicated background thread.
    """
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
    