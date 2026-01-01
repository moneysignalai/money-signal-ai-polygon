"""IV Crush options bot

Identifies option contracts with significant implied volatility drops (IV crush)
on liquid underlyings, typically post-earnings or catalysts.
"""

import json
import os
import time
from typing import Dict

from bots.options_common import OptionContract, iter_option_contracts, options_flow_allow_outside_rth
from bots.shared import (
    DEBUG_FLOW_REASONS,
    chart_link,
    debug_filter_reason,
    in_rth_window_est,
    resolve_options_underlying_universe,
)
from bots.status_report import record_bot_stats

BOT_NAME = "options_iv_crush"

IVCRUSH_MAX_DTE = int(os.getenv("IVCRUSH_MAX_DTE", "21"))
IVCRUSH_MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "25"))
IVCRUSH_MIN_VOL = int(os.getenv("IVCRUSH_MIN_VOL", "100"))
OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5"))
IV_CACHE_PATH = os.getenv("OPTIONS_IV_CACHE_PATH", "/tmp/options_iv_cache.json")


def _load_iv_cache() -> Dict[str, float]:
    try:
        if os.path.exists(IV_CACHE_PATH):
            with open(IV_CACHE_PATH, "r") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): float(v) for k, v in data.items() if v is not None}
    except Exception:
        pass
    return {}


def _save_iv_cache(cache: Dict[str, float]) -> None:
    try:
        with open(IV_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception:
        print(f"[options_iv_crush] failed to persist IV cache to {IV_CACHE_PATH}")


async def run_options_iv_crush() -> None:
    start = time.perf_counter()
    scanned = 0
    matches = 0
    alerts = 0
    reason_counts: Dict[str, int] = {}

    if not options_flow_allow_outside_rth() and not in_rth_window_est():
        record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
        return

    universe = await resolve_options_underlying_universe(BOT_NAME)
    print(f"[options_iv_crush] universe_size={len(universe)}")
    if not universe:
        record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)
        return

    iv_cache = _load_iv_cache()
    updated_cache = dict(iv_cache)

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
                    debug_filter_reason(BOT_NAME, c.contract, "ivcrush_underlying_price_too_low")
                    continue
                if c.dte is not None and c.dte > IVCRUSH_MAX_DTE:
                    reason_counts["dte"] = reason_counts.get("dte", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "ivcrush_dte_too_long")
                    continue
                if c.volume is None or c.volume < IVCRUSH_MIN_VOL:
                    reason_counts["volume"] = reason_counts.get("volume", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "ivcrush_volume_too_low")
                    continue
                if c.iv is None:
                    reason_counts["iv_missing"] = reason_counts.get("iv_missing", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "ivcrush_missing_iv")
                    continue

                prev_iv = iv_cache.get(c.contract)
                updated_cache[c.contract] = c.iv
                if prev_iv is None:
                    reason_counts["iv_baseline"] = reason_counts.get("iv_baseline", 0) + 1
                    continue
                if prev_iv <= 0:
                    continue
                iv_drop_pct = (prev_iv - c.iv) / prev_iv * 100.0
                if iv_drop_pct < IVCRUSH_MIN_IV_DROP_PCT:
                    reason_counts["iv_drop"] = reason_counts.get("iv_drop", 0) + 1
                    debug_filter_reason(BOT_NAME, c.contract, "ivcrush_iv_drop_too_small")
                    continue

                matches += 1
                from bots.shared import send_alert

                text = (
                    f"• Contract: {c.contract}\n"
                    f"• IV: {prev_iv:.1f}% → {c.iv:.1f}% (drop {iv_drop_pct:.1f}%)\n"
                    f"• Volume: {c.volume or 0} | DTE: {c.dte if c.dte is not None else 'n/a'}\n"
                    f"• Underlying: ${c.underlying_price or 0:.2f}\n"
                    f"• {chart_link(symbol)}"
                )
                alerts += 1
                send_alert("IV CRUSH", symbol, c.underlying_price or 0.0, 0.0, extra=text)
        except Exception as exc:
            debug_filter_reason(BOT_NAME, symbol, f"error {exc}")
            continue

    runtime = time.perf_counter() - start
    _save_iv_cache(updated_cache)
    if DEBUG_FLOW_REASONS and matches == 0:
        print(f"[options_iv_crush] No alerts. Filter breakdown: {reason_counts}")
    record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)

