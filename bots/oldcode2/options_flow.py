"""Legacy options_flow wrapper

This module is kept for backward compatibility. The unified options flow logic
has been split into four dedicated bots:
  • options_cheap_flow
  • options_unusual_flow
  • options_whales
  • options_iv_crush

Calls to run_options_flow() now log a deprecation notice and return after
recording zero stats to keep heartbeat visibility stable without double-counting
the new bots.
"""

import time

from bots.status_report import record_bot_stats

BOT_NAME = "options_flow"


async def run_options_flow() -> None:
    start = time.perf_counter()
    print(
        "[options_flow] DEPRECATED — use options_cheap_flow / options_unusual_flow / options_whales / options_iv_crush"
    )
    record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)


async def run_bot() -> None:
    await run_options_flow()

