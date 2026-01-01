"""Deprecated intraday_flow stub.

Volume Monster, Panic Flush, and Momentum Reversal now run as dedicated bots.
This module remains only for backward compatibility; the scheduler no longer
invokes it directly. If invoked manually, it records zero stats and exits.
"""

from __future__ import annotations

import time

from bots.status_report import record_bot_stats


def should_run_now():
    return False, "deprecated"


async def run_intraday_flow() -> None:
    start = time.perf_counter()
    print("[intraday_flow] deprecated stub; use dedicated bots instead")
    record_bot_stats("intraday_flow", 0, 0, 0, time.perf_counter() - start)
