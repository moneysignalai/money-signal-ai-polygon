"""Unusual Flow options bot

Flags option contracts with notable size/notional that stand out from typical
flow but are not necessarily full whale-sized orders.
"""

import os
import time
from typing import Dict

from bots.options_common import iter_option_contracts, options_flow_allow_outside_rth
from bots.shared import (
    DEBUG_FLOW_REASONS,
    chart_link,
    debug_filter_reason,
    in_rth_window_est,
    resolve_options_underlying_universe,
)
from bots.status_report import record_bot_stats

BOT_NAME = "options_unusual_flow"

UNUSUAL_MIN_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))
UNUSUAL_MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "15000"))
UNUSUAL_MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))
OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5"))


async def run_options_unusual_flow() -> None:
    start = time.perf_counter()
    scanned = 0
    matches = 0
    alerts = 0
    reason_counts: Dict[str, int] = {}

    if not options_flow_allow_outside_rth() and not in_rth_window_est():
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
        return

    universe = await resolve_options_underlying_universe(BOT_NAME)
    print(f"[options_unusual_flow] universe_size={len(universe)}")
    if not universe:
        record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
        return

    for symbol in universe:
        scanned += 1
        try:
            contracts = iter_option_contracts(symbol)
            if not contracts:
                reason_counts["no_chain"] = reason_counts.get("no_chain", 0) + 1
                debug_filter_reason(BOT_NAME, symbol, "no_chain_data")
                continue
            for c in contracts:
                if c.underlying_price is not None and c.underlying_price < OPTIONS_MIN_UNDERLYING_PRICE:
                    reason_counts["underlying_price"] = reason_counts.get("underlying_price", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "unusual_underlying_price_too_low")
                    continue
                if c.dte is not None and c.dte > UNUSUAL_MAX_DTE:
                    reason_counts["dte"] = reason_counts.get("dte", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "unusual_dte_too_long")
                    continue
                if c.notional is None or c.size is None:
                    reason_counts["missing_prices"] = reason_counts.get("missing_prices", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "unusual_missing_price_size")
                    continue
                if c.notional < UNUSUAL_MIN_NOTIONAL or c.size < UNUSUAL_MIN_SIZE:
                    reason_counts["size_notional"] = reason_counts.get("size_notional", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "unusual_notional_or_size_too_low")
                    continue

                matches += 1
                from bots.shared import send_alert

                text = (
                    f"• Contract: {c.contract}\n"
                    f"• Size: {c.size} | Notional: ${c.notional:,.0f} | DTE: {c.dte if c.dte is not None else 'n/a'}\n"
                    f"• Underlying: ${c.underlying_price or 0:.2f}\n"
                    f"• {chart_link(symbol)}"
                )
                alerts += 1
                send_alert("UNUSUAL FLOW", symbol, c.underlying_price or 0.0, 0.0, extra=text)
        except Exception as exc:
            debug_filter_reason(BOT_NAME, symbol, f"error {exc}")
            continue

    runtime = time.perf_counter() - start
    if DEBUG_FLOW_REASONS and matches == 0:
        print(f"[options_unusual_flow] No alerts. Filter breakdown: {reason_counts}")
    record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)

