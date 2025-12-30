"""FastAPI app and background scheduler for MoneySignalAI bots."""

import asyncio
import importlib
import os
import threading
import time
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

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))
BOT_TIMEOUT_SECONDS = int(os.getenv("BOT_TIMEOUT_SECONDS", "180"))
STATUS_HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))


def _parse_bot_list(env_var: str) -> set[str]:
    raw = os.getenv(env_var, "")
    return {b.strip().lower() for b in raw.replace(" ", "").split(",") if b.strip()}


def _interval_env(bot_name: str, default: int) -> int:
    key = f"{bot_name.upper()}_INTERVAL"
    try:
        v = int(os.getenv(key, str(default)))
        return max(5, v)
    except Exception:
        return default


DISABLED_BOTS = _parse_bot_list("DISABLED_BOTS")
TEST_MODE_BOTS = _parse_bot_list("TEST_MODE_BOTS")


# ----------------- Bot registry -----------------
# (public_name, module_path, function_name, default_interval_seconds)

BOT_DEFS: List[Tuple[str, str, str, int]] = [
    ("premarket", "bots.premarket", "run_premarket", 60),
    ("equity_flow", "bots.equity_flow", "run_equity_flow", 60),
    ("intraday_flow", "bots.intraday_flow", "run_intraday_flow", 60),
    ("rsi_signals", "bots.rsi_signals", "run_rsi_signals", 60),
    (
        "opening_range_breakout",
        "bots.openingrangebreakout",
        "run_opening_range_breakout",
        20,
    ),
    ("options_flow", "bots.options_flow", "run_options_flow", 60),
    ("options_indicator", "bots.options_indicator", "run_options_indicator", 60),
    ("squeeze", "bots.squeeze", "run_squeeze", 60),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar", 60),
    ("trend_flow", "bots.trend_flow", "run_trend_flow", 60),
    ("earnings", "bots.earnings", "run_earnings", 300),
    ("daily_ideas", "bots.daily_ideas", "run_daily_ideas", 900),
    ("status_report", "bots.status_report", "run_status", int(STATUS_HEARTBEAT_INTERVAL_MIN * 60)),
    ("debug_ping", "bots.debug_ping", "run_debug_ping", 300),
    ("debug_status_ping", "bots.debug_status_ping", "run_debug_status_ping", 300),
]

# Precompute effective intervals
BOTS: List[Tuple[str, str, str, int]] = []
for name, mod, func, base_interval in BOT_DEFS:
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
            for (name, module, func, interval) in BOTS
        ],
    }


@app.get("/health")
async def health():
    summary = None
    try:
        from bots.status_report import _load_stats

        data = _load_stats()
        summary = data.get("bots", {}) if isinstance(data, dict) else None
    except Exception:
        summary = None

    return {"status": "healthy", "now_est": now_est_str(), "summary": summary}


# ----------------- Scheduler logic -----------------

def _should_run_bot(name: str) -> bool:
    lname = name.lower()
    if lname in DISABLED_BOTS:
        return False
    if TEST_MODE_BOTS and lname not in TEST_MODE_BOTS:
        return False
    return True


async def _run_single_bot(public_name: str, module_path: str, func_name: str, record_error):
    try:
        module = importlib.import_module(module_path)
        func = getattr(module, "run_bot", None) or getattr(module, func_name, None)
        if func is None:
            raise AttributeError(f"{module_path} has no attribute run_bot or {func_name}")

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

    try:
        from bots.status_report import record_error
    except Exception as e:
        print(f"[main] WARNING: could not import bots.status_report: {e}")
        record_error = None  # type: ignore

    next_run_ts: Dict[str, float] = {name: 0.0 for name, _, _, _ in BOTS}

    while True:
        cycle_start_ts = time.time()
        cycle_start_dt = datetime.fromtimestamp(cycle_start_ts, tz=eastern)
        print(f"[main] scheduler cycle starting at {cycle_start_dt.strftime('%H:%M:%S')} ET")

        tasks: List[asyncio.Task] = []
        for name, module_path, func_name, interval in BOTS:
            if not _should_run_bot(name):
                continue

            due_ts = next_run_ts.get(name, 0.0)
            if cycle_start_ts >= due_ts:
                print(f"[main] scheduling bot {name} (interval={interval}s)")
                tasks.append(
                    asyncio.create_task(
                        _run_single_bot(name, module_path, func_name, record_error)
                    )
                )
                next_run_ts[name] = cycle_start_ts + interval

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        cycle_end_ts = time.time()
        elapsed = cycle_end_ts - cycle_start_ts
        print(
            f"[main] scheduler cycle finished in {elapsed:.2f}s; "
            f"sleeping {base_interval_seconds}s"
        )
        await asyncio.sleep(base_interval_seconds)


def _start_background_scheduler() -> None:
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
