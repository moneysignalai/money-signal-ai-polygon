"""Deprecated trend_flow wrapper.

This module previously combined trend breakout and swing pullback logic. The
functionality now lives in dedicated bots:

• bots/trend_rider.py (breakouts in strong uptrends)
• bots/swing_pullback.py (dip-buy setups within strong trends)

The scheduler should run those bots directly. This wrapper simply records a
zero-stat run to remain backward compatible with any lingering imports.
"""

from __future__ import annotations

import time

from bots.status_report import record_bot_stats

BOT_NAME = "trend_flow"
STRATEGY_TAG = "TREND_FLOW"


async def run_trend_flow() -> None:
    """No-op stub preserved for backward compatibility."""

    start = time.perf_counter()
    print("[trend_flow] DEPRECATED – use Trend Rider and Swing Pullback bots instead")
    runtime = time.perf_counter() - start
    record_bot_stats(BOT_NAME, 0, 0, 0, runtime)


async def run_bot() -> None:  # legacy alias
    await run_trend_flow()

