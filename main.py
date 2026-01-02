"""FastAPI app and background scheduler for MoneySignalAI bots."""

import asyncio
import importlib
import os
import threading
import time
import traceback
from datetime import datetime
from typing import Dict, List, Tuple

import pytz
import uvicorn
from fastapi import FastAPI

from bots.shared import in_premarket_window_est, in_rth_window_est, is_trading_day_est

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
    ("volume_monster", "bots.volume_monster", "run_volume_monster", 60),
    ("gap_flow", "bots.gap_flow", "run_gap_flow", 60),
    ("swing_pullback", "bots.swing_pullback", "run_swing_pullback", 60),
    ("panic_flush", "bots.panic_flush", "run_panic_flush", 60),
    ("momentum_reversal", "bots.momentum_reversal", "run_momentum_reversal", 60),
    ("trend_rider", "bots.trend_rider", "run_trend_rider", 60),
    ("rsi_signals", "bots.rsi_signals", "run_rsi_signals", 60),
    (
        "opening_range_breakout",
        "bots.openingrangebreakout",
        "run_opening_range_breakout",
        20,
    ),
    ("options_cheap_flow", "bots.options_cheap_flow", "run_options_cheap_flow", 60),
    ("options_unusual_flow", "bots.options_unusual_flow", "run_options_unusual_flow", 60),
    ("options_whales", "bots.options_whales", "run_options_whales", 60),
    ("options_iv_crush", "bots.options_iv_crush", "run_options_iv_crush", 60),
    ("options_indicator", "bots.options_indicator", "run_options_indicator", 60),
    ("squeeze", "bots.squeeze", "run_squeeze", 60),
    ("dark_pool_radar", "bots.dark_pool_radar", "run_dark_pool_radar", 60),
    ("earnings", "bots.earnings", "run_earnings", 300),
    ("daily_ideas", "bots.daily_ideas", "run_daily_ideas", 900),
    ("status_report", "bots.status_report", "run_status", int(STATUS_HEARTBEAT_INTERVAL_MIN * 60)),
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


def _validate_registry() -> None:
    """Eagerly import bot modules to surface missing entrypoints early."""
    for name, module_path, func_name, _ in BOTS:
        try:
            module = importlib.import_module(module_path)
            func = getattr(module, "run_bot", None) or getattr(module, func_name, None)
            if func is None:
                print(
                    f"[main] WARNING registry mismatch: {module_path} missing run_bot/{func_name}"
                )
            elif not asyncio.iscoroutinefunction(func):
                print(
                    f"[main] WARNING registry mismatch: {module_path}.{func_name} is not async"
                )
        except Exception as exc:
            print(f"[main] WARNING failed to validate {name} ({module_path}): {exc}")


# ----------------- Scheduler logic -----------------

def _skip_reason(name: str) -> str | None:
    """Return a human-readable skip reason for a bot if applicable."""
    lname = name.lower()
    if lname in DISABLED_BOTS:
        return "disabled via DISABLED_BOTS"
    if TEST_MODE_BOTS and lname not in TEST_MODE_BOTS:
        return "not in TEST_MODE_BOTS while test mode is active"
    return None


def _time_window_allows(name: str, module_path: str) -> Tuple[bool, str | None]:
    """
    Combine trading-day/time-of-day heuristics with a bot-provided should_run_now().
    This keeps scheduler visibility aligned with per-bot gating.
    """

    lname = name.lower()

    # Trading-day guard (Mon–Fri only); allow status_report to always run
    if lname != "status_report" and not is_trading_day_est():
        return False, "non-trading day"

    # RTH-only bots
    rth_bots = {
        "volume_monster",
        "gap_flow",
        "swing_pullback",
        "trend_rider",
        "panic_flush",
        "momentum_reversal",
        "rsi_signals",
        "options_cheap_flow",
        "options_unusual_flow",
        "options_whales",
        "options_iv_crush",
        "options_indicator",
        "squeeze",
        "dark_pool_radar",
    }

    # Premarket-only
    if lname == "premarket":
        if os.getenv("PREMARKET_ALLOW_OUTSIDE_WINDOW", "false").lower() == "true":
            pass
        elif not in_premarket_window_est():
            return False, "outside premarket window"

    if lname in rth_bots:
        allow_outside = os.getenv(f"{lname.upper()}_ALLOW_OUTSIDE_RTH", "false").lower()
        if allow_outside != "true" and not in_rth_window_est():
            return False, "outside RTH window"

    if lname in {"opening_range_breakout", "orb"}:
        allow_outside = os.getenv("ORB_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
        if not allow_outside:
            if not in_rth_window_est():
                return False, "outside RTH window"
            try:
                limit = int(os.getenv("ORB_RANGE_MINUTES", "15"))
            except Exception:
                limit = 15
            # Only allow within the opening range window
            if not in_rth_window_est(0, limit):
                return False, "outside ORB window"

    # Delegate to bot-specific should_run_now if available
    try:
        module = importlib.import_module(module_path)
        fn = getattr(module, "should_run_now", None)
        if fn is None:
            return True, None

        result = fn()
        if isinstance(result, tuple):
            allowed, reason = result
            return bool(allowed), reason
        return bool(result), None
    except Exception as exc:
        print(f"[scheduler] warning checking time window for {module_path}: {exc}")
        return True, None


async def _run_single_bot(
    public_name: str,
    module_path: str,
    func_name: str,
    record_error,
    record_stats=None,
):
    start_dt = datetime.now(eastern)
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
        tb = traceback.format_exc()
        print(
            f"[bot_runner] ERROR bot={public_name} fn={module_path}.{func_name} "
            f"exc={e.__class__.__name__} msg={e}\n{tb}"
        )
        if record_error is not None:
            try:
                record_error(public_name, e)
            except Exception as inner:
                print("[main] ERROR while recording bot error:", inner)
        # Record a failed run so status shows attempted today
        if record_stats is not None:
            try:
                finished_dt = datetime.now(eastern)
                runtime = max((finished_dt - start_dt).total_seconds(), 0.0)
                record_stats(
                    public_name,
                    scanned=0,
                    matched=0,
                    alerts=0,
                    runtime_seconds=runtime,
                    started_at=start_dt,
                    finished_at=finished_dt,
                )
            except Exception as inner:
                print(f"[bot_runner] warning recording failure stats for {public_name}: {inner}")


async def scheduler_loop(base_interval_seconds: int = SCAN_INTERVAL_SECONDS):
    print(
        f"[main] scheduler_loop starting with base_interval={base_interval_seconds}s, "
        f"bot_timeout={BOT_TIMEOUT_SECONDS}s"
    )

    try:
        from bots.status_report import record_error
        from bots.shared import record_bot_stats, today_est_date
    except Exception as e:
        print(f"[main] WARNING: could not import status helpers: {e}")
        record_error = None  # type: ignore
        record_bot_stats = None  # type: ignore
        today_est_date = None  # type: ignore

    next_run_ts: Dict[str, float] = {name: 0.0 for name, _, _, _ in BOTS}
    last_skip_day: Dict[str, str] = {}

    while True:
        try:
            cycle_start_ts = time.time()
            cycle_start_dt = datetime.fromtimestamp(cycle_start_ts, tz=eastern)
            print(
                f"[main] scheduler cycle starting at {cycle_start_dt.strftime('%H:%M:%S')} ET"
            )

            tasks: List[asyncio.Task] = []
            for name, module_path, func_name, interval in BOTS:
                skip = _skip_reason(name)
                if skip:
                    print(f"[scheduler] bot={name} action=SKIPPED_DISABLED reason={skip}")
                    continue

                allowed, reason = _time_window_allows(name, module_path)
                if not allowed:
                    print(
                        f"[scheduler] bot={name} action=SKIPPED_TIME_WINDOW "
                        f"reason={reason or 'time window closed'}"
                    )
                    # Record a zero-scan skip once per trading day so status doesn't show "no run"
                    if record_bot_stats and today_est_date:
                        day_key = today_est_date().isoformat()
                        last_key = last_skip_day.get(name)
                        if last_key != day_key:
                            try:
                                record_bot_stats(
                                    name,
                                    scanned=0,
                                    matched=0,
                                    alerts=0,
                                    runtime_seconds=0.0,
                                )
                                last_skip_day[name] = day_key
                            except Exception as exc:
                                print(
                                    f"[scheduler] warning recording skip stats for {name}: {exc}"
                                )
                    next_run_ts[name] = cycle_start_ts + interval
                    continue

                due_ts = next_run_ts.get(name, 0.0)
                if cycle_start_ts >= due_ts:
                    print(f"[scheduler] bot={name} action=RUN interval={interval}s")
                    tasks.append(
                        asyncio.create_task(
                            _run_single_bot(
                                name,
                                module_path,
                                func_name,
                                record_error,
                                record_stats=record_bot_stats,
                            )
                        )
                    )
                    next_run_ts[name] = cycle_start_ts + interval
                else:
                    wait_for = max(0.0, due_ts - cycle_start_ts)
                    print(f"[scheduler] bot={name} action=WAITING next_in={wait_for:.1f}s")

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            cycle_end_ts = time.time()
            elapsed = cycle_end_ts - cycle_start_ts
            print(
                f"[main] scheduler cycle finished in {elapsed:.2f}s; "
                f"sleeping {base_interval_seconds}s"
            )
        except Exception as exc:
            print(f"[main] scheduler loop error: {exc}")
        await asyncio.sleep(base_interval_seconds)


def _start_background_scheduler() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(scheduler_loop())


@app.on_event("startup")
async def startup_event():
    print(f"[main] startup_event fired at {now_est_str()}")
    _validate_registry()
    print(
        f"[main] launching background scheduler thread "
        f"(base_interval={SCAN_INTERVAL_SECONDS}s, bot_timeout={BOT_TIMEOUT_SECONDS}s)"
    )
    threading.Thread(target=_start_background_scheduler, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
