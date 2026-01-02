import asyncio
import asyncio
import sys
import time
import types

import pytest

import main
from bots import shared


def test_scheduler_no_overlap(monkeypatch, tmp_path):
    calls = []

    async def slow_bot():
        calls.append(("start", time.time()))
        await asyncio.sleep(0.2)
        calls.append(("end", time.time()))

    module = types.SimpleNamespace(run_bot=slow_bot)
    sys.modules["bots.test_bot"] = module

    monkeypatch.setenv("STATUS_STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(main, "BOTS", [("test_bot", "bots.test_bot", "run_bot", 1)])

    start = time.time()
    asyncio.run(main.scheduler_loop(base_interval_seconds=0.05, stop_after_cycles=2))
    elapsed = time.time() - start

    # Bot should have run once and not overlapped
    assert len([c for c in calls if c[0] == "start"]) == 1
    assert elapsed < 0.2 + 0.1  # scheduler cycles should not wait for bot runtime


def test_scheduler_non_blocking_tick(monkeypatch, tmp_path):
    async def slow_bot():
        await asyncio.sleep(0.2)

    module = types.SimpleNamespace(run_bot=slow_bot)
    sys.modules["bots.test_bot2"] = module

    monkeypatch.setenv("STATUS_STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(main, "BOTS", [("test_bot2", "bots.test_bot2", "run_bot", 1)])

    start = time.time()
    asyncio.run(main.scheduler_loop(base_interval_seconds=0.05, stop_after_cycles=2))
    elapsed = time.time() - start
    assert elapsed < 0.3  # two scheduler ticks should finish even while bot runs


def test_each_bot_records_stats(monkeypatch, tmp_path):
    async def bot_one():
        shared.record_bot_stats("bot_one", 1, 1, 1, runtime_seconds=0.01)

    async def bot_two():
        shared.record_bot_stats("bot_two", 2, 1, 0, runtime_seconds=0.01)

    sys.modules["bots.bot_one"] = types.SimpleNamespace(run_bot=bot_one)
    sys.modules["bots.bot_two"] = types.SimpleNamespace(run_bot=bot_two)

    monkeypatch.setenv("STATUS_STATS_PATH", str(tmp_path / "stats.json"))
    monkeypatch.setattr(main, "BOTS", [
        ("bot_one", "bots.bot_one", "run_bot", 1),
        ("bot_two", "bots.bot_two", "run_bot", 1),
    ])

    asyncio.run(main.scheduler_loop(base_interval_seconds=0.05, stop_after_cycles=1))

    from bots.status_report import _load_stats

    stats = _load_stats().get("bots", {})
    assert set(stats.keys()) >= {"bot_one", "bot_two"}


def test_heartbeat_contains_all_bots(monkeypatch, tmp_path):
    monkeypatch.setenv("STATUS_STATS_PATH", str(tmp_path / "stats.json"))
    from bots.status_report import _save_stats

    now = shared.now_est_dt()
    data = {
        "bots": {
            "alpha": {
                "latest": {
                    "scanned": 1,
                    "matched": 1,
                    "alerts": 0,
                    "runtime": 0.1,
                    "finished_at_ts": now.timestamp(),
                    "finished_at_str": shared.format_est_timestamp(now),
                    "trading_day": now.date().isoformat(),
                }
            },
            "beta": {
                "latest": {
                    "scanned": 2,
                    "matched": 1,
                    "alerts": 1,
                    "runtime": 0.2,
                    "finished_at_ts": now.timestamp(),
                    "finished_at_str": shared.format_est_timestamp(now),
                    "trading_day": now.date().isoformat(),
                }
            },
        },
        "errors": [],
        "last_heartbeat_ts": 0.0,
    }
    _save_stats(data)

    from bots import status_report

    hb = status_report._format_heartbeat()
    assert "alpha" in hb.lower()
    assert "beta" in hb.lower()


def test_universe_cap_enforced(monkeypatch):
    monkeypatch.setenv("TICKER_UNIVERSE", ",".join([f"SYM{i}" for i in range(30)]))
    monkeypatch.setenv("UNIVERSE_HARD_CAP", "10")

    universe = shared.resolve_universe_for_bot("tester", apply_dynamic_filters=False)
    assert len(universe) == 10
