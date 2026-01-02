import asyncio
import importlib
import sys
import importlib
import time
from types import ModuleType
from difflib import SequenceMatcher

import pytest

import main
from bots import shared
from bots.bot_meta import BOT_METADATA, get_strategy_tag


@pytest.mark.anyio
async def test_scheduler_no_overlap(monkeypatch):
    run_count = 0

    async def bot_runner():
        nonlocal run_count
        run_count += 1
        await asyncio.sleep(0.2)

    module = ModuleType("fake_bot")
    module.run_bot = bot_runner
    sys.modules[module.__name__] = module

    monkeypatch.setattr(
        main,
        "BOTS",
        [("test_bot", module.__name__, "run_bot", 0)],
    )
    monkeypatch.setattr(main, "is_trading_day_est", lambda: True)
    monkeypatch.setenv("INTEGRATION_TEST", "true")

    await main.scheduler_loop(base_interval_seconds=0.05, stop_after_cycles=3)
    assert run_count == 1  # no overlap allowed while still running


@pytest.mark.anyio
async def test_scheduler_non_blocking_tick(monkeypatch):
    start_times = []

    async def slow_bot():
        start_times.append(time.time())
        await asyncio.sleep(0.3)

    module = ModuleType("slow_bot_mod")
    module.run_bot = slow_bot
    sys.modules[module.__name__] = module

    monkeypatch.setattr(main, "BOTS", [("slow_bot", module.__name__, "run_bot", 0)])
    monkeypatch.setattr(main, "is_trading_day_est", lambda: True)
    monkeypatch.setenv("INTEGRATION_TEST", "true")

    start = time.time()
    await main.scheduler_loop(base_interval_seconds=0.05, stop_after_cycles=2)
    duration = time.time() - start
    # loop should complete quickly even with long-running bot
    assert duration < 1.0
    assert len(start_times) == 1


def test_universe_hard_cap_enforced(monkeypatch):
    monkeypatch.setenv("UNIVERSE_HARD_CAP", "5")
    monkeypatch.setenv("UNIVERSE_TOP_N", "100")
    monkeypatch.setenv("TICKER_UNIVERSE", ",".join([f"T{i}" for i in range(20)]))
    monkeypatch.setenv("TEST_MODE", "true")
    universe = shared.resolve_universe_for_bot("test_bot")
    assert len(universe) <= 5


def test_each_bot_records_stats(tmp_path, monkeypatch):
    stats_path = tmp_path / "stats.json"
    monkeypatch.setenv("STATUS_STATS_PATH", str(stats_path))
    monkeypatch.setattr(shared, "STATS_PATH", str(stats_path))
    data = shared.record_bot_stats(
        "test_bot",
        scanned=10,
        matched=2,
        alerts=1,
        runtime_seconds=1.2,
    )
    assert "latest" in data["bots"]["test_bot"]
    assert data["bots"]["test_bot"]["latest"]["alerts"] == 1


def test_alert_templates_include_required_fields():
    for name, meta in BOT_METADATA.items():
        assert meta.strategy_tag
        assert meta.title_template
        assert meta.why_template
        # ensure tags appear inside templates
        assert meta.strategy_tag in meta.title_template or meta.strategy_tag in meta.why_template


def test_bot_uniqueness_tags():
    tags = [meta.strategy_tag for meta in BOT_METADATA.values()]
    assert len(tags) == len(set(tags))

    metas = list(BOT_METADATA.items())
    for i in range(len(metas)):
        name_a, meta_a = metas[i]
        for j in range(i + 1, len(metas)):
            name_b, meta_b = metas[j]
            combo_a = f"{meta_a.title_template} {meta_a.why_template}"
            combo_b = f"{meta_b.title_template} {meta_b.why_template}"
            ratio = SequenceMatcher(None, combo_a, combo_b).ratio()
            assert ratio < 0.7, f"{name_a} and {name_b} too similar"


def test_strategy_tag_constants_unique():
    tags: dict[str, str] = {}

    for public_name, module_path, *_ in main.BOTS:
        module = importlib.import_module(module_path)
        tag = getattr(module, "STRATEGY_TAG", None)
        assert tag, f"{public_name} missing STRATEGY_TAG"

        if tag in tags:
            raise AssertionError(f"Duplicate STRATEGY_TAG {tag} for {public_name} and {tags[tag]}")
        tags[tag] = public_name

        meta = BOT_METADATA.get(public_name)
        if meta:
            assert meta.strategy_tag == tag, f"{public_name} STRATEGY_TAG mismatch with BOT_METADATA"
