"""Cheap Flow options bot

Scans option chains for low-premium contracts that still carry meaningful size
and notional, useful for spotting lotto-style or high-leverage positioning.
Runs during RTH unless overridden via OPTIONS_FLOW_ALLOW_OUTSIDE_RTH.
"""

import os
import time
from typing import Dict

from bots.options_common import (
    OptionContract,
    format_cheap_option_alert,
    iter_option_contracts,
    options_flow_allow_outside_rth,
    send_option_alert,
)
from bots.shared import (
    DEBUG_FLOW_REASONS,
    debug_filter_reason,
    in_rth_window_est,
    now_est_dt,
    resolve_options_underlying_universe,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "options_cheap_flow"

CHEAP_MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "0.60"))
CHEAP_MIN_NOTIONAL = float(os.getenv("CHEAP_MIN_NOTIONAL", "3000"))
CHEAP_MIN_SIZE = int(os.getenv("CHEAP_MIN_SIZE", "5"))
OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5"))
CHEAP_MIN_DTE = os.getenv("CHEAP_MIN_DTE")
CHEAP_MAX_DTE = os.getenv("CHEAP_MAX_DTE")


def _dte_in_range(contract: OptionContract) -> bool:
    if contract.dte is None:
        return True
    if CHEAP_MIN_DTE is not None:
        try:
            if contract.dte < int(CHEAP_MIN_DTE):
                return False
        except Exception:
            pass
    if CHEAP_MAX_DTE is not None:
        try:
            if contract.dte > int(CHEAP_MAX_DTE):
                return False
        except Exception:
            pass
    return True


async def run_options_cheap_flow() -> None:
    start_perf = time.perf_counter()
    start_dt = now_est_dt()
    scanned = 0
    matches = 0
    alerts = 0
    reason_counts: Dict[str, int] = {}

    allow_outside = options_flow_allow_outside_rth()
    if not allow_outside and not in_rth_window_est():
        finished = now_est_dt()
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0, started_at=start_dt, finished_at=finished)
        return

    universe = await resolve_options_underlying_universe(BOT_NAME)
    print(f"[options_cheap_flow] universe_size={len(universe)}")
    if not universe:
        finished = now_est_dt()
        record_bot_stats(
            BOT_NAME, 0, 0, 0, time.perf_counter() - start_perf, started_at=start_dt, finished_at=finished
        )
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
                    debug_filter_reason(BOT_NAME, c.contract, "cheap_underlying_price_too_low")
                    continue
                if c.premium is None or c.size is None or c.notional is None:
                    reason_counts["missing_prices"] = reason_counts.get("missing_prices", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "cheap_missing_price_size")
                    continue
                if c.premium > CHEAP_MAX_PREMIUM:
                    reason_counts["premium"] = reason_counts.get("premium", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "cheap_premium_too_high")
                    continue
                if c.size < CHEAP_MIN_SIZE or c.notional < CHEAP_MIN_NOTIONAL:
                    reason_counts["size_notional"] = reason_counts.get("size_notional", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "cheap_size_notional_too_small")
                    continue
                if not _dte_in_range(c):
                    reason_counts["dte"] = reason_counts.get("dte", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "cheap_dte_out_of_range")
                    continue

                matches += 1
                alert_text = format_cheap_option_alert(
                    contract=c,
                    premium_cap=CHEAP_MAX_PREMIUM,
                    min_notional=CHEAP_MIN_NOTIONAL,
                    min_size=CHEAP_MIN_SIZE,
                    chart_symbol=symbol,
                )
                alerts += 1
                send_option_alert(alert_text)
        except Exception as exc:
            debug_filter_reason(BOT_NAME, symbol, f"error {exc}")
            record_error(BOT_NAME, exc)
            continue

    finished = now_est_dt()
    runtime = time.perf_counter() - start_perf
    if DEBUG_FLOW_REASONS and matches == 0:
        print(f"[options_cheap_flow] No alerts. Filter breakdown: {reason_counts}")
    record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime, started_at=start_dt, finished_at=finished)

