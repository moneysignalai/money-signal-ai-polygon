"""Lightweight smoke test runner for MoneySignalAI bots.

Sets helpful debug overrides, trims universes, and sequentially calls each bot
entrypoint to ensure imports and RTH gating do not crash. This is not a unit
test; it is intended for manual validation while developing.
"""
from __future__ import annotations

import asyncio
import os
from typing import Tuple

# Force verbose filter reasons and allow outside-RTH execution for testing
os.environ.setdefault("DEBUG_FLOW_REASONS", "true")
os.environ.setdefault("OPTIONS_FLOW_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("OPTIONS_INDICATOR_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("EQUITY_FLOW_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("INTRADAY_FLOW_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("RSI_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("SQUEEZE_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("TREND_FLOW_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("DARK_POOL_ALLOW_OUTSIDE_RTH", "true")
os.environ.setdefault("PREMARKET_ALLOW_OUTSIDE_WINDOW", "true")

# Tighten universes to keep runs quick
os.environ.setdefault("OPTIONS_FLOW_TICKER_UNIVERSE", "SPY,QQQ,AAPL,NVDA,TSLA")
os.environ.setdefault("SQUEEZE_TICKER_UNIVERSE", "SPY,QQQ,AAPL,NVDA,TSLA")
os.environ.setdefault("PREMARKET_TICKER_UNIVERSE", "SPY,QQQ,AAPL,NVDA,TSLA")

import importlib

from main import BOTS, BOT_TIMEOUT_SECONDS  # noqa: E402


async def _call_bot(module_path: str, func_name: str) -> Tuple[str, bool, str]:
    """Attempt to import and execute a bot entrypoint."""
    try:
        module = importlib.import_module(module_path)
        func = getattr(module, "run_bot", None) or getattr(module, func_name)
        if not asyncio.iscoroutinefunction(func):
            return func_name, False, "function is not async"
        await asyncio.wait_for(func(), timeout=BOT_TIMEOUT_SECONDS)
        return func_name, True, "ok"
    except Exception as exc:  # pragma: no cover - manual smoke utility
        return func_name, False, str(exc)


async def main() -> None:
    results: list[Tuple[str, bool, str]] = []
    for name, module_path, func_name, _ in BOTS:
        outcome = await _call_bot(module_path, func_name)
        results.append(outcome)
        status = "PASS" if outcome[1] else "FAIL"
        print(f"[{status}] {name} -> {outcome[2]}")

    failures = [r for r in results if not r[1]]
    if failures:
        print("\nFailures detected:")
        for func_name, _, msg in failures:
            print(f" - {func_name}: {msg}")
    else:
        print("\nAll bots executed without uncaught exceptions.")


if __name__ == "__main__":
    asyncio.run(main())
