"""Deprecated wrapper for legacy equity_flow.

This module previously combined several equity scanners. The logic has been
split into dedicated bots:
- Volume Monster (bots.volume_monster)
- Gap Scanner (bots.gap_scanner)
- Swing Pullback (bots.swing_pullback)

The scheduled registry now points to those dedicated bots. This wrapper keeps a
backward-compatible async entrypoint and will call the new bots sequentially
when invoked directly.
"""

import asyncio
import time

from bots.status_report import record_bot_stats

from .volume_monster import run_volume_monster
from .gap_scanner import run_gap_scanner
from .swing_pullback import run_swing_pullback


async def run_equity_flow() -> None:
    start = time.perf_counter()
    scanned = matched = alerts = 0
    try:
        # Delegate to the split bots; stats are recorded individually inside
        # each bot, so this wrapper only records its own aggregate attempts.
        await run_volume_monster()
        await run_gap_scanner()
        await run_swing_pullback()
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"[equity_flow] wrapper error: {exc}")
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats("equity_flow", scanned, matched, alerts, runtime)
