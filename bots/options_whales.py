"""Whale Flow options bot

Surfaces very large option trades with significant size and notional (big-money
positioning). Runs during RTH with the shared options universe resolver.
"""

import os
import time

from bots.options_common import (
    FlowReasonTracker,
    format_whale_option_alert,
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

BOT_NAME = "options_whales"

WHALES_MIN_SIZE = int(os.getenv("WHALES_MIN_SIZE", "50"))
WHALES_MIN_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "150000"))
WHALES_MAX_DTE = int(os.getenv("WHALES_MAX_DTE", "120"))
OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5"))


async def run_options_whales() -> None:
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
    print(f"[options_whales] universe_size={len(universe)}")
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
                tracker.record(symbol, "whale_no_chain_data")
                continue
            for c in contracts:
                if c.underlying_price is not None and c.underlying_price < OPTIONS_MIN_UNDERLYING_PRICE:
                    tracker.record(c.contract, "whale_underlying_price_too_low")
                    continue
                if c.dte is not None and c.dte > WHALES_MAX_DTE:
                    tracker.record(c.contract, "whale_dte_too_long")
                    continue
                if c.notional is None or c.size is None:
                    suffix = c.price_size_reason or "missing_price_size"
                    tracker.record(c.contract, f"whale_{suffix}")
                    continue
                if c.notional < WHALES_MIN_NOTIONAL or c.size < WHALES_MIN_SIZE:
                    tracker.record(c.contract, "whale_notional_or_size_too_low")
                    continue

                matches += 1

                flow_tags = ["WHALE_SIZE"]
                if c.dte is not None and c.dte <= 14:
                    flow_tags.append("SHORT_DTE")
                if c.notional and c.notional >= WHALES_MIN_NOTIONAL * 2:
                    flow_tags.append("MEGA_NOTIONAL")

                bias_line = "Aggressive bullish whale flow" if (c.cp or "").upper().startswith("C") else "Aggressive bearish whale flow"

                oi = c.open_interest or 0
                vol = c.volume or 0
                context_parts = []
                if vol:
                    context_parts.append(f"Option volume {vol}")
                if oi:
                    ratio = vol / oi if oi else 0
                    context_parts.append(f"OI {oi} ({ratio:.1f}× OI)")
                context_line = " vs ".join(context_parts) if context_parts else "volume/OI context unavailable"

                alert_text = format_whale_option_alert(
                    contract=c,
                    flow_tags=flow_tags,
                    context_line=context_line,
                    bias_line=bias_line,
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
    tracker.log_summary()
    record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime, started_at=start_dt, finished_at=finished)

