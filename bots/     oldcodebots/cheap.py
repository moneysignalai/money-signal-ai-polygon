# bots/cheap.py
#
# "Cheap lottos" options flow bot.
#
# Looks for relatively low-premium call sweeps with decent size / notional.
# Designed to be robust against missing data from Polygon (no float/NoneType math).

import os
from datetime import datetime, date
from typing import List, Dict, Any, Optional

from bots.shared import (
    POLYGON_KEY,
    send_alert,
    chart_link,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    get_option_chain_cached,
    get_last_option_trades_cached,
    today_est_date,
    minutes_since_midnight_est,
    now_est,  # string, used only for display
)

# ------------- CONFIG -------------

# Only scan during RTH (09:30–16:00 ET)
RTH_START_MIN = 9 * 60 + 30
RTH_END_MIN = 16 * 60

# Cheap contract definition (more aggressive defaults, env-tunable)
CHEAP_MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "0.55"))        # max price per contract
CHEAP_MIN_SIZE = int(os.getenv("CHEAP_MIN_SIZE", "50"))                  # min contracts
CHEAP_MIN_NOTIONAL = float(os.getenv("CHEAP_MIN_NOTIONAL", "5000"))      # min notional dollars

# Max number of underlyings to scan per cycle
MAX_UNIVERSE = int(os.getenv("CHEAP_MAX_UNIVERSE", "80"))


def _in_rth_window() -> bool:
    mins = minutes_since_midnight_est()
    return RTH_START_MIN <= mins <= RTH_END_MIN


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_int(x: Any) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except (TypeError, ValueError):
        return None


def _compute_dte(expiration_str: Optional[str]) -> Optional[int]:
    if not expiration_str:
        return None
    try:
        exp_date = datetime.strptime(expiration_str, "%Y-%m-%d").date()
        today: date = today_est_date()
        return (exp_date - today).days
    except Exception:
        return None


async def run_cheap() -> None:
    """
    Scan a liquid universe for cheap call sweeps.

    Logic:
      • Only run during RTH.
      • Universe: TICKER_UNIVERSE env or dynamic top-volume universe (truncated to MAX_UNIVERSE).
      • For each underlying:
          - Skip ETFs (from shared ETF blacklist).
          - Get Polygon snapshot option chain.
          - For each option:
              • Filter to CALLs with last trade:
                    - 0 < price <= CHEAP_MAX_PREMIUM
                    - size >= CHEAP_MIN_SIZE
                    - notional >= CHEAP_MIN_NOTIONAL
              • Send a unified-style alert.
    """
    if not POLYGON_KEY:
        print("[cheap] POLYGON_KEY missing; skipping.")
        return

    if not _in_rth_window():
        print("[cheap] outside RTH; skipping.")
        return

    # Build universe
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        universe = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        universe = get_dynamic_top_volume_universe(max_tickers=MAX_UNIVERSE, volume_coverage=0.90)

    if not universe:
        print("[cheap] empty universe; skipping.")
        return

    now_str = now_est()  # human-readable string from shared

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        results = chain.get("results") or []
        if not isinstance(results, list):
            continue

        for opt in results:
            # Defensive extraction
            details: Dict[str, Any] = opt.get("details") or {}
            contract = details.get("ticker")
            if not contract:
                continue

            contract_type = (details.get("contract_type") or "").upper()
            if contract_type != "CALL":
                continue

            exp_str = details.get("expiration_date")
            dte = _compute_dte(exp_str)

            # Get last trade for this specific contract
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            t_res = trade.get("results") or {}
            if isinstance(t_res, list):
                if not t_res:
                    continue
                t_res = t_res[0]
            elif not isinstance(t_res, dict):
                continue

            last_price = _safe_float(t_res.get("p") or t_res.get("price"))
            size = _safe_int(t_res.get("s") or t_res.get("size"))

            if last_price is None or size is None:
                continue
            if last_price <= 0 or size <= 0:
                continue
            if last_price > CHEAP_MAX_PREMIUM:
                continue

            notional = last_price * size * 100  # standard option multiplier
            if notional < CHEAP_MIN_NOTIONAL or size < CHEAP_MIN_SIZE:
                continue

            # At this point, we have a qualifying cheap lotto trade
            notional_rounded = round(notional)

            extra_lines = [
                f"🧨 CHEAP — {sym}",
                f"🕒 {now_str}",
                f"💰 Trade Price: ${last_price:.2f}",
                "────────────",
                "🎯 Cheap CALL Lotto",
                f"📌 Contract: {contract}",
                f"📦 Size: {size}",
                f"💰 Notional: ≈ ${notional_rounded:,.0f}",
            ]
            if dte is not None:
                extra_lines.append(f"🗓️ DTE: {dte}")

            extra_lines.append(f"🔗 Chart: {chart_link(sym)}")

            extra_text = "\n".join(extra_lines)

            # rvol is unknown here; pass 0.0 (we mostly care about the body text)
            send_alert("cheap", sym, last_price, 0.0, extra=extra_text)

    print("[cheap] scan complete.")
