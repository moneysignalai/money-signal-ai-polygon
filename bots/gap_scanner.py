"""Deprecated alias for gap flow logic.

The active gap-up/gap-down scanner now lives in bots.gap_flow. This wrapper
remains only for backward compatibility with any lingering imports.
"""

BOT_NAME = "gap_scanner"
STRATEGY_TAG = "GAP_SCANNER"

from bots.gap_flow import run_gap_flow as run_gap_scanner, run_bot  # noqa: F401

__all__ = ["run_gap_scanner", "run_bot"]
