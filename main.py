import asyncio
import importlib
import os
import threading
from datetime import datetime
from typing import Dict, List, Tuple

import pytz
import uvicorn
from fastapi import FastAPI

# ----------------- Time helpers -----------------

eastern = pytz.timezone("US/Eastern")


def now_est_str() -> str:
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d").lstrip("0")


# ----------------- Global config -----------------

# Default global base polling interval (used as minimum sleep)
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))
BOT_TIMEOUT_SECONDS = int(os.getenv("BOT_TIMEOUT_SECONDS", "40"))

# Per-bot interval can be overridden via env:
#   <BOTNAME_UPPER>_INTERVAL, e.g. OPTIONS_FLOW_INTERVAL=20
def _interval_env(bot_name: str, default: int) -> int:
    key = f"{bot_name.upper()}_INTERVAL"
    try:
        v = int(os.getenv(key, str(default)))
        return max(5, v)  # safety: minimum 5s
    except Exception:
        return default


# DISABLED_BOTS: comma-separated list, e.g. "daily_ideas,options_indicator"
DISABLED_BOTS = {
    b.strip().lower()
    for b in os.getenv("DISABLED_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# TEST_MODE_BOTS: comma-separated list of bots that should be considered "test only"
TEST_MODE_BOTS = {
    b.strip().lower()
    for b in os.getenv("TEST_MODE_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# ----------------- Bot registry -----------------
# (public_name, module_path, function_name, default_interval_seconds)

_BOT_DEFS: List[Tuple[str, str, str, int]] = [
    ("premarket", "bots.premarket", "run_premarket", 60),
    ("equity_flow", "bots.equity_flow", "run_equity_flow", 20),
    ("intraday_flow", "bots.intraday_flow", "run_intraday_flow", 20),
    ("rsi_signals", "bots.rsi_signals", "run_rsi_signals", 20),
    ("opening_range_breakout", "bots.openingrangebreakout", "run_opening_range_breakout", 20),
    ("options_flow", "bots.options_flow", "run_options_flow", 20),
    ("options_indicator", "bots.options_indicator", "run_options_indicator", 60),
    ("squeeze", "bots.squeeze", "run_squeeze", 60),
    ("earnings", "bots.earnings", "run_earnings", 300),
    ("trend_flow", "bots.trend_flow", "run_trend_flow", 60),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar", 60),
    ("daily_ideas", "bots.daily_ideas", "run_daily_ideas", 600),
]

# Final BOTS list with resolved intervals (after env overrides & disabled filter)
# (public_name, module_path, function_name, interval_seconds)
BOTS: List[Tuple[str, str, str, int]] = []
for name, mod, func, base_interval in _BOT_DEFS:
    if name.lower() in DISABLED_BOTS:
        continue
    interval = _interval_env(name, base_interval)
    BOTS.append((name, mod, func, interval))


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
        "bots": [
            {
                "name": name,
                "module": module,
                "func": func,
                "interval": interval,
                "disabled": name.lower() in DISABLED_BOTS,
                "test_mode": name.lower() in TEST_MODE_BOTS,
            }
            for (name, module, func, interval) in _BOT_DEFS
        ],
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
        func = getattr(module, "run_bot", None) or getattr(module, func_name, None)
        if func is None:
            raise AttributeError(
                f"{module_path} has no attribute run_bot or {func_name}"
            )

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


async def scheduler_loop(base_interval_seconds: int = SCAN_INTERVAL_SECONDS):
    """
    Main async loop that runs bots on their own cadence.

    Each bot has its own interval, but we tick the loop every `base_interval_seconds`.
    """
    print(
        f"[main] scheduler_loop starting with base_interval={base_interval_seconds}s, "
        f"bot_timeout={BOT_TIMEOUT_SECONDS}s"
    )

    # Lazy import status_report helpers so we can log errors & send heartbeat
    try:
        from bots.status_report import run_status, record_error
    except Exception as e:
        print(f"[main] WARNING: could not import bots.status_report: {e}")
        run_status = None  # type: ignore
        record_error = None  # type: ignore

    # Per-bot next-run timestamps
    next_run_ts: Dict[str, float] = {}
    time_now = datetime.now(eastern).timestamp()
    for name, _, _, interval in BOTS:
        # Run everything once on startup
        next_run_ts[name] = time_now

    # Log configuration
    print("[main] Bot configuration:")
    for name, module_path, func_name, interval in _BOT_DEFS:
        state = []
        if name.lower() in DISABLED_BOTS:
            state.append("DISABLED")
        if name.lower() in TEST_MODE_BOTS:
            state.append("TEST_MODE")
        state_str = f" ({', '.join(state)})" if state else ""
        effective_interval = _interval_env(name, interval)
        print(f"  - {name}: {module_path}.{func_name}, interval={effective_interval}s{state_str}")

    import time

    while True:
        cycle_start_dt = datetime.now(eastern)
        cycle_start_ts = cycle_start_dt.timestamp()
        print(f"[main] scheduler cycle starting at {cycle_start_dt.strftime('%H:%M:%S')} ET")

        tasks: List[asyncio.Task] = []

        # 1) schedule bots whose next_run_ts is due
        for name, module_path, func_name, interval in BOTS:
            due_ts = next_run_ts.get(name, 0.0)
            if cycle_start_ts >= due_ts:
                print(f"[main] scheduling bot {name} (interval={interval}s)")
                tasks.append(
                    asyncio.create_task(
                        _run_single_bot(name, module_path, func_name, record_error)
                    )
                )
                next_run_ts[name] = cycle_start_ts + interval

        # 2) schedule status_report.run_status, if available
        if run_status is not None:
            try:
                print("[main] scheduling status heartbeat task")
                tasks.append(asyncio.create_task(run_status()))
            except Exception as e:
                print(f"[main] ERROR scheduling run_status: {e}")

        # 3) wait for all tasks to complete (errors logged inside tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        cycle_end_dt = datetime.now(eastern)
        elapsed = (cycle_end_dt - cycle_start_dt).total_seconds()
        print(f"[main] scheduler cycle finished in {elapsed:.2f}s; sleeping {base_interval_seconds}s")

        await asyncio.sleep(base_interval_seconds)


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
        f"(base_interval={SCAN_INTERVAL_SECONDS}s, bot_timeout={BOT_TIMEOUT_SECONDS}s)"
    )
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
