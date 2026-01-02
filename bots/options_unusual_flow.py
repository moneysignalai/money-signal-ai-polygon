"""Unusual Flow options bot

Flags option contracts with notable size/notional that stand out from typical
flow but are not necessarily full whale-sized orders.
"""

import os
import time

from bots.options_common import (
    FlowReasonTracker,
    format_unusual_option_alert,
    iter_option_contracts,
    options_flow_allow_outside_rth,
    send_option_alert,
)
from bots.shared import (
    debug_filter_reason,
    in_rth_window_est,
    now_est_dt,
    resolve_options_underlying_universe,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "options_unusual_flow"

UNUSUAL_MIN_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))
UNUSUAL_MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "15000"))
UNUSUAL_MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))
OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5"))


async def run_options_unusual_flow() -> None:
    start_perf = time.perf_counter()
    start_dt = now_est_dt()
    scanned = 0
    matches = 0
    alerts = 0
    tracker = FlowReasonTracker(BOT_NAME)

    if not options_flow_allow_outside_rth() and not in_rth_window_est():
        finished = now_est_dt()
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0, started_at=start_dt, finished_at=finished)
        return

    universe = await resolve_options_underlying_universe(BOT_NAME)
    print(f"[options_unusual_flow] universe_size={len(universe)}")
    if not universe:
        finished = now_est_dt()
        record_bot_stats(
            BOT_NAME, 0, 0, 0, time.perf_counter() - start_perf, started_at=start_dt, finished_at=finished
        )
        return

    for symbol in universe:
        scanned += 1
        try:
            contracts = iter_option_contracts(symbol, reason_tracker=tracker)
            if not contracts:
                tracker.record(symbol, "unusual_no_chain_data")
                continue
            for c in contracts:
                if c.underlying_price is None or c.underlying_price <= 0:
                    tracker.record(c.contract, "unusual_underlying_price_missing")
                    continue
                if c.underlying_price < OPTIONS_MIN_UNDERLYING_PRICE:
                    tracker.record(c.contract, "unusual_underlying_price_too_low")
                    continue
                if c.dte is not None and c.dte > UNUSUAL_MAX_DTE:
                    tracker.record(c.contract, "unusual_dte_too_long")
                    continue
                if c.notional is None or c.size is None:
                    suffix = c.price_size_reason or "missing_price_size"
                    tracker.record(c.contract, f"unusual_{suffix}")
                    continue
                if c.notional < UNUSUAL_MIN_NOTIONAL or c.size < UNUSUAL_MIN_SIZE:
                    tracker.record(c.contract, "unusual_notional_or_size_too_low")
                    continue

                matches += 1
                flow_tags = []
                if c.size and c.size >= UNUSUAL_MIN_SIZE:
                    flow_tags.append("SIZE")
                if c.notional and c.notional >= UNUSUAL_MIN_NOTIONAL:
                    flow_tags.append("NOTIONAL")
                if c.cp:
                    flow_tags.append(c.cp.upper())
                if c.dte is not None:
                    if c.dte <= 7:
                        flow_tags.append("NEAR_TERM")
                    elif c.dte <= 21:
                        flow_tags.append("MID_TERM")

                narrative = ""
                if c.cp and c.cp.upper().startswith("C"):
                    narrative = "Upside call flow well above normal activity."
                elif c.cp and c.cp.upper().startswith("P"):
                    narrative = "Downside put flow well above normal activity."
                else:
                    narrative = "Unusual flow above baseline volume."

                alert_text = format_unusual_option_alert(
                    contract=c,
                    flow_tags=flow_tags,
                    volume_today=c.volume,
                    trade_size=c.size,
                    chart_symbol=symbol,
                    narrative=narrative,
                )
                alerts += 1
                send_option_alert(alert_text)
        except Exception as exc:
            debug_filter_reason(BOT_NAME, symbol, f"error {exc}")
            record_error(BOT_NAME, exc)
            continue

    finished = now_est_dt()
    runtime = time.perf_counter() - start_perf
    tracker.log_summary()
    record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime, started_at=start_dt, finished_at=finished)

