"""Deprecated wrapper for legacy equity_flow.

All logic now lives in dedicated bots:
• Volume Monster (bots.volume_monster)
• Gap Flow (bots.gap_flow)
• Swing Pullback (bots.swing_pullback)

This stub remains only for backward compatibility before the file moves to
oldcode/. It records a zero-stat run and logs a deprecation notice.
"""

import time

from bots.status_report import record_bot_stats

BOT_NAME = "equity_flow"
STRATEGY_TAG = "EQUITY_FLOW"


async def run_equity_flow() -> None:
    start = time.perf_counter()
    print("[equity_flow] DEPRECATED – use Volume Monster / Gap Flow / Swing Pullback")
    record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
