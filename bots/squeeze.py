# bots/squeeze.py
#
# "Squeeze" options flow bot.
#
# Heuristic: look for larger, higher-premium call sweeps that could indicate
# a short/gamma squeeze style move. Focus is on robustness (no crashes)
# and unified alert formatting.

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

# Squeeze-style contract thresholds
SQUEEZE_MIN_PREMIUM = 0.50       # minimum option price
SQUEEZE_MAX_PREMIUM = 10.00      # cap so we avoid crazy misprints
SQUEEZE_MIN_SIZE = 200           # min contracts
SQUEEZE_MIN_NOTIONAL = 150_000   # min notional dollars

# Max number of underlyings to scan per cycle
MAX_UNIVERSE = 60


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


async def run_squeeze() -> None:
    """
    Scan a liquid universe for squeeze-style call sweeps.

    Logic:
      • Only run during RTH.
      • Universe: TICKER_UNIVERSE env or dynamic top-volume universe (truncated).
      • For each underlying:
          - Skip ETFs.
          - Get Polygon snapshot option chain.
          - For each option:
              • Filter to CALLs with last trade:
                    - SQUEEZE_MIN_PREMIUM <= price <= SQUEEZE_MAX_PREMIUM
                    - size >= SQUEEZE_MIN_SIZE
                    - notional >= SQUEEZE_MIN_NOTIONAL
              • Send a unified-style alert.
    """
    if not POLYGON_KEY:
        print("[squeeze] POLYGON_KEY missing; skipping.")
        return

    if not _in_rth_window():
        print("[squeeze] outside RTH; skipping.")
        return

    # Build universe
    env = None
    try:
        import os
        env = os.getenv("TICKER_UNIVERSE")
    except Exception:
        env = None

    if env:
        universe = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        universe = get_dynamic_top_volume_universe(max_tickers=MAX_UNIVERSE, volume_coverage=0.90)

    if not universe:
        print("[squeeze] empty universe; skipping.")
        return

    now_str = now_est()  # string from shared, used only in text

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
            details: Dict[str, Any] = opt.get("details") or {}
            contract = details.get("ticker")
            if not contract:
                continue

            contract_type = (details.get("contract_type") or "").upper()
            if contract_type != "CALL":
                continue

            exp_str = details.get("expiration_date")
            dte = _compute_dte(exp_str)

            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            t_res = trade.get("results") or {}
            last_price = _safe_float(t_res.get("p") or t_res.get("price"))
            size = _safe_int(t_res.get("s") or t_res.get("size"))

            if last_price is None or size is None:
                continue
            if last_price <= 0 or size <= 0:
                continue
            if last_price < SQUEEZE_MIN_PREMIUM or last_price > SQUEEZE_MAX_PREMIUM:
                continue

            notional = last_price * size * 100
            if notional < SQUEEZE_MIN_NOTIONAL or size < SQUEEZE_MIN_SIZE:
                continue

            notional_rounded = round(notional)

            extra_lines = [
                f"🧨 SQUEEZE — {sym}",
                f"🕒 {now_str}",
                f"💰 Trade Price: ${last_price:.2f}",
                "────────────",
                "🧲 High-Notional CALL Flow (possible squeeze)",
                f"📌 Contract: {contract}",
                f"📦 Size: {size}",
                f"💰 Notional: ≈ ${notional_rounded:,.0f}",
            ]
            if dte is not None:
                extra_lines.append(f"🗓️ DTE: {dte}")

            extra_lines.append(f"🔗 Chart: {chart_link(sym)}")

            extra_text = "\n".join(extra_lines)

            # rvol not computed here; set to 0.0 just for interface
            send_alert("squeeze", sym, last_price, 0.0, extra=extra_text)

    print("[squeeze] scan complete.")