import os
import threading
import asyncio
import importlib
from datetime import datetime
from typing import Dict, Tuple, List

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

def _interval_env(bot_name: str, default: int) -> int:
    key = f"{bot_name.upper()}_INTERVAL"
    try:
        v = int(os.getenv(key, str(default)))
        return max(5, v)
    except Exception:
        return default


DISABLED_BOTS = {
    b.strip().lower()
    for b in os.getenv("DISABLED_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

TEST_MODE_BOTS = {
    b.strip().lower()
    for b in os.getenv("TEST_MODE_BOTS", "").replace(" ", "").split(",")
    if b.strip()
}

# ----------------- Bot registry -----------------

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
    try:
        module = importlib.import_module(module_path)
        func = getattr(module, func_name, None)
        if func is None:
            raise AttributeError(f"{module_path} has no attribute {func_name}")

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
    print(
        f"[main] scheduler_loop starting with base_interval={base_interval_seconds}s, "
        f"bot_timeout={BOT_TIMEOUT_SECONDS}s"
    )

    # Import status helpers
    try:
        from bots.status_report import (
            run_status,
            record_error,
            send_heartbeat,
            HEARTBEAT_MIN_INTERVAL_MIN,
        )
    except Exception as e:
        print(f"[main] WARNING: failed to import status_report: {e}")
        run_status = None   # type: ignore
        send_heartbeat = None  # type: ignore
        record_error = None  # type: ignore

    last_heartbeat_ts = 0

    # Next-run timestamps
    next_run_ts: Dict[str, float] = {}
    now_ts = datetime.now(eastern).timestamp()
    for name, _, _, interval in BOTS:
        next_run_ts[name] = now_ts

    print("[main] Bot configuration:")
    for name, module_path, func_name, interval in _BOT_DEFS:
        labels = []
        if name.lower() in DISABLED_BOTS:
            labels.append("DISABLED")
        if name.lower() in TEST_MODE_BOTS:
            labels.append("TEST_MODE")
        label_txt = f" ({', '.join(labels)})" if labels else ""
        effective_interval = _interval_env(name, interval)
        print(f"  - {name}: {module_path}.{func_name}, interval={effective_interval}s{label_txt}")

    import time

    # MAIN LOOP
    while True:
        cycle_start_dt = datetime.now(eastern)
        cycle_start_ts = cycle_start_dt.timestamp()
        print(f"[main] scheduler cycle starting at {cycle_start_dt.strftime('%H:%M:%S')} ET")

        tasks: List[asyncio.Task] = []

        # Run due bots
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

        # Status report (detailed per cycle)
        if run_status is not None:
            try:
                tasks.append(asyncio.create_task(run_status()))
            except Exception as e:
                print(f"[main] ERROR scheduling run_status: {e}")

        # HEARTBEAT (every X minutes)
        if send_heartbeat is not None:
            if HEARTBEAT_MIN_INTERVAL_MIN > 0:
                now_ts = time.time()
                if now_ts - last_heartbeat_ts >= HEARTBEAT_MIN_INTERVAL_MIN * 60:
                    try:
                        print("[main] sending heartbeat...")
                        send_heartbeat()
                        last_heartbeat_ts = now_ts
                    except Exception as e:
                        print(f"[main] ERROR sending heartbeat: {e}")

        # Wait for all tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        cycle_end_dt = datetime.now(eastern)
        elapsed = (cycle_end_dt - cycle_start_dt).total_seconds()
        print(f"[main] scheduler cycle finished in {elapsed:.2f}s; sleeping {base_interval_seconds}s")

        await asyncio.sleep(base_interval_seconds)


def _start_background_scheduler():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    print(f"[main] startup_event fired at {now_est_str()}")
    print(
        f"[main] launching background scheduler thread "
        f"(base_interval={SCAN_INTERVAL_SECONDS}s, bot_timeout={BOT_TIMEOUT_SECONDS}s)"
    )
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))