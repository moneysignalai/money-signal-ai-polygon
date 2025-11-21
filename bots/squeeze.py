# bots/squeeze.py — Options SQUEEZE flow bot

import os
from datetime import datetime, date
from typing import List, Dict, Any, Optional

import pytz

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
    now_est,  # human-readable EST time string
)

eastern = pytz.timezone("US/Eastern")

# ------------- CONFIG -------------

# Only scan during RTH (09:30–16:00 ET)
RTH_START_MIN = 9 * 60 + 30
RTH_END_MIN = 16 * 60

# Squeeze-style contract thresholds (tweak via env if you want more/less aggressive)
SQUEEZE_MIN_PREMIUM = float(os.getenv("SQUEEZE_MIN_PREMIUM", "0.50"))      # min option price
SQUEEZE_MAX_PREMIUM = float(os.getenv("SQUEEZE_MAX_PREMIUM", "15.00"))     # cap to avoid bad prints
SQUEEZE_MIN_SIZE = int(os.getenv("SQUEEZE_MIN_SIZE", "200"))               # min contracts
SQUEEZE_MIN_NOTIONAL = float(os.getenv("SQUEEZE_MIN_NOTIONAL", "150000"))  # min notional $

# Max number of underlyings to scan per cycle
MAX_UNIVERSE = int(os.getenv("SQUEEZE_MAX_UNIVERSE", "60"))

# Per-day de-dupe
_alert_date: Optional[date] = None
_alerted_contracts: set[str] = set()


# ---------------- STATE ----------------

def _reset_day() -> None:
    global _alert_date, _alerted_contracts
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    return contract in _alerted_contracts


def _mark(contract: str) -> None:
    _alerted_contracts.add(contract)


def _in_rth_window() -> bool:
    mins = minutes_since_midnight_est()
    return RTH_START_MIN <= mins <= RTH_END_MIN


# ---------------- HELPERS ----------------

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


# ---------------- MAIN BOT ----------------

async def run_squeeze() -> None:
    """
    SQUEEZE options flow bot.

    Looks for relatively high-premium, high-size CALL sweeps that can signal
    short/gamma squeeze behavior.

    Logic:
      • Only run during RTH (09:30–16:00 ET).
      • Universe:
            - SQUEEZE_TICKER_UNIVERSE env (if set)
            - else TICKER_UNIVERSE env
            - else dynamic top-volume universe (MAX_UNIVERSE names).
      • For each underlying:
          - Skip ETFs.
          - Get Polygon snapshot option chain.
          - For each option:
              • CALLs only
              • SQUEEZE_MIN_PREMIUM <= option price <= SQUEEZE_MAX_PREMIUM
              • size >= SQUEEZE_MIN_SIZE
              • notional >= SQUEEZE_MIN_NOTIONAL
              • DTE between 0 and 60 days
          - One alert per contract per day.
    """
    _reset_day()

    if not POLYGON_KEY:
        print("[squeeze] POLYGON_KEY missing; skipping.")
        return

    if not _in_rth_window():
        print("[squeeze] outside RTH; skipping.")
        return

    # Build universe
    env = os.getenv("SQUEEZE_TICKER_UNIVERSE") or os.getenv("TICKER_UNIVERSE")
    if env:
        universe = [t.strip().upper() for t in env.split(",") if t.strip()]
    else:
        universe = get_dynamic_top_volume_universe(
            max_tickers=MAX_UNIVERSE,
            volume_coverage=0.90,
        )

    if not universe:
        print("[squeeze] empty universe; skipping.")
        return

    now_str = now_est()  # already like "10:48 AM EST · Nov 20"

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
            if _already_alerted(contract):
                continue

            contract_type = (details.get("contract_type") or "").upper()
            if contract_type != "CALL":
                continue

            exp_str = details.get("expiration_date")
            dte = _compute_dte(exp_str)
            if dte is None or dte < 0 or dte > 60:
                # focus on short- to mid-dated contracts
                continue

            # Last trade for this contract
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            t_res = trade.get("results") or {}
            if isinstance(t_res, list):
                if not t_res:
                    continue
                last = t_res[0]
            elif isinstance(t_res, dict):
                last = t_res
            else:
                continue

            last_price = _safe_float(last.get("p") or last.get("price"))
            size = _safe_int(last.get("s") or last.get("size"))

            if last_price is None or size is None:
                continue
            if last_price <= 0 or size <= 0:
                continue
            if last_price < SQUEEZE_MIN_PREMIUM or last_price > SQUEEZE_MAX_PREMIUM:
                continue

            notional = last_price * size * 100  # standard option multiplier
            if notional < SQUEEZE_MIN_NOTIONAL or size < SQUEEZE_MIN_SIZE:
                continue

            notional_rounded = round(notional)

            extra_lines = [
                f"🧨 SQUEEZE — {sym}",
                f"🕒 {now_str}",
                f"💰 Trade Price: ${last_price:.2f}",
                "────────────",
                "🧲 High-Notional CALL Sweep (possible squeeze)",
                f"📌 Contract: {contract}",
                f"📦 Size: {size}",
                f"💰 Notional: ≈ ${notional_rounded:,.0f}",
            ]
            if dte is not None:
                extra_lines.append(f"🗓️ DTE: {dte}")

            extra_lines.append(f"🔗 Chart: {chart_link(sym)}")

            extra_text = "\n".join(extra_lines)

            # rvol not computed here; we care about flow, so pass 0.0
            send_alert("squeeze", sym, last_price, 0.0, extra=extra_text)
            _mark(contract)

    print("[squeeze] scan complete.")