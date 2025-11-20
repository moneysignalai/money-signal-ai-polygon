# bots/cheap.py — Cheap 0–3 DTE options flow bot
#
# Hunts for:
#   • 0–3 DTE CALLs & PUTs
#   • Underlying in a reasonable price band (defaults: $10–$80)
#   • Cheap premium (defaults: <= $0.40)
#   • Real flow: min volume + min notional
#
# One alert per contract per day, formatted in the premium Telegram style.

import os
from datetime import date, datetime
from typing import Any, Dict, Optional, List, Tuple

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    chart_link,
    now_est,
    get_option_chain_cached,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG ----------------

# Underlying filters
MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "10.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "80.0"))

# Option filters
MAX_CHEAP_DTE = int(os.getenv("CHEAP_MAX_DTE", "3"))  # 0–3 DTE by default
MAX_OPTION_PRICE = float(os.getenv("CHEAP_MAX_OPTION_PRICE", "0.40"))  # premium <= $0.40
MIN_OPTION_VOLUME = float(os.getenv("CHEAP_MIN_OPTION_VOLUME", "200"))  # contracts
MIN_OPTION_NOTIONAL = float(os.getenv("CHEAP_MIN_OPTION_NOTIONAL", "10000"))  # $10k+

# Time window (RTH only: 09:30–16:00 ET)
CHEAP_START_MIN = 9 * 60 + 30
CHEAP_END_MIN = 16 * 60

# Daily de-dupe (per-contract)
_alert_date: Optional[date] = None
_alerted_contracts: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_contracts
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_contracts = set()


def _already_alerted_contract(contract: str) -> bool:
    _reset_if_new_day()
    return contract in _alerted_contracts


def _mark_alerted_contract(contract: str) -> None:
    _reset_if_new_day()
    _alerted_contracts.add(contract)


def _in_cheap_window() -> bool:
    """Only scan 09:30–16:00 ET on weekdays."""
    now_et = datetime.now(eastern)
    if now_et.weekday() >= 5:  # 0=Mon, 6=Sun
        return False
    mins = now_et.hour * 60 + now_et.minute
    return CHEAP_START_MIN <= mins <= CHEAP_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _parse_dte(expiration: Optional[str], today: date) -> Optional[int]:
    if not expiration:
        return None
    try:
        # expiration_date is usually "YYYY-MM-DD"
        exp_d = datetime.strptime(expiration, "%Y-%m-%d").date()
        return (exp_d - today).days
    except Exception:
        return None


def _extract_underlying_price(opt: Dict[str, Any]) -> Optional[float]:
    ua = opt.get("underlying_asset") or {}
    cand = ua.get("price") or ua.get("underlying_price") or opt.get("underlying_price")
    try:
        if cand is None:
            return None
        px = float(cand)
        if px <= 0:
            return None
        return px
    except Exception:
        return None


def _extract_option_metrics(opt: Dict[str, Any]) -> Tuple[Optional[float], float, float]:
    """
    Returns (last_price, volume, notional_per_100) where:
      - last_price may be None if we can't infer a reasonable price.
      - volume is contract volume.
      - notional_per_100 = last_price * volume * 100 (if last_price known) else 0.
    """
    day = opt.get("day") or {}
    last_trade = opt.get("last_trade") or {}

    # Try last trade price first
    price = last_trade.get("p")
    if price is None:
        price = day.get("vw") or day.get("c") or day.get("o")

    try:
        last_price = float(price) if price is not None else None
    except Exception:
        last_price = None

    vol_raw = day.get("v") or day.get("volume") or 0
    try:
        volume = float(vol_raw)
    except Exception:
        volume = 0.0

    if last_price is None or last_price <= 0 or volume <= 0:
        return None, 0.0, 0.0

    notional = last_price * volume * 100.0
    return last_price, volume, notional


def _moneyness_label(
    opt_type: str, strike: float, underlying_price: float
) -> Tuple[str, float]:
    """
    Returns (label, pct_distance) where label is "ITM" or "OTM"
    and pct_distance is abs(strike - underlying) / underlying in %.
    """
    if underlying_price <= 0:
        return ("", 0.0)

    dist_pct = abs(strike - underlying_price) / underlying_price * 100.0

    if opt_type.lower() == "call":
        label = "ITM" if strike < underlying_price else "OTM"
    else:  # put
        label = "ITM" if strike > underlying_price else "OTM"

    return label, dist_pct


async def run_cheap():
    """
    Cheap 0–3 DTE options bot.

    Logic:
      • Runs 09:30–16:00 ET on weekdays.
      • Universe: dynamic top-volume stocks (or TICKER_UNIVERSE if set).
      • For each underlying:
          - Underlying price between MIN_UNDERLYING_PRICE and MAX_UNDERLYING_PRICE.
          - Fetch snapshot option chain (via get_option_chain_cached).
          - For each option result:
              • Contract type: CALL or PUT
              • DTE in [0, MAX_CHEAP_DTE]
              • Option last/avg price <= MAX_OPTION_PRICE
              • Volume >= MIN_OPTION_VOLUME
              • Notional >= MIN_OPTION_NOTIONAL
          - Alert top cheap contract by notional for that underlying, per scan.
      • De-dupe: each contract symbol alerts at most once per day.
    """
    if not POLYGON_KEY or not _client:
        print("[cheap] POLYGON_KEY not set or client not initialized; skipping.")
        return

    if not _in_cheap_window():
        print("[cheap] Outside 09:30–16:00 window; skipping scan.")
        return

    _reset_if_new_day()
    today = date.today()
    universe = _get_universe()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        results = chain.get("results") or chain.get("options") or []
        if not results:
            continue

        best_candidate: Optional[Dict[str, Any]] = None
        best_notional = 0.0

        for opt in results:
            details = opt.get("details") or {}
            opt_type = (details.get("contract_type") or "").lower()
            if opt_type not in ("call", "put"):
                continue

            exp_str = details.get("expiration_date")
            dte = _parse_dte(exp_str, today)
            if dte is None or dte < 0 or dte > MAX_CHEAP_DTE:
                continue

            strike_raw = details.get("strike_price")
            try:
                strike = float(strike_raw)
            except Exception:
                continue

            underlying_price = _extract_underlying_price(opt)
            if underlying_price is None:
                continue

            if (
                underlying_price < MIN_UNDERLYING_PRICE
                or underlying_price > MAX_UNDERLYING_PRICE
            ):
                continue

            last_price, volume, notional = _extract_option_metrics(opt)
            if last_price is None or last_price <= 0:
                continue
            if last_price > MAX_OPTION_PRICE:
                continue
            if volume < MIN_OPTION_VOLUME:
                continue
            if notional < MIN_OPTION_NOTIONAL:
                continue

            contract_symbol = opt.get("ticker") or opt.get("option_symbol")
            if not contract_symbol:
                continue
            contract_symbol = str(contract_symbol)

            if _already_alerted_contract(contract_symbol):
                continue

            # Option passed all filters; track best by notional for this underlying
            if notional > best_notional:
                best_notional = notional
                best_candidate = {
                    "contract": contract_symbol,
                    "opt_type": opt_type,
                    "dte": dte,
                    "strike": strike,
                    "under_px": underlying_price,
                    "last_px": last_price,
                    "volume": volume,
                    "notional": notional,
                }

        if not best_candidate:
            continue

        # Build and send alert for best candidate
        c = best_candidate
        contract = c["contract"]
        opt_type = c["opt_type"]
        dte = c["dte"]
        strike = c["strike"]
        under_px = c["under_px"]
        last_px = c["last_px"]
        volume = c["volume"]
        notional = c["notional"]

        label, dist_pct = _moneyness_label(opt_type, strike, under_px)

        emoji = "🎯" if opt_type == "call" else "🛡️"
        dir_word = "CALL" if opt_type == "call" else "PUT"

        body = (
            f"{emoji} Cheap {dir_word}: {contract}\n"
            f"📌 Underlying {sym}: ≈ ${under_px:.2f}\n"
            f"🎯 Strike: {strike:.2f} · DTE: {dte} · {label} {dist_pct:.1f}% from spot\n"
            f"💵 Option Price: ${last_px:.2f} (≤ ${MAX_OPTION_PRICE:.2f} cheap filter)\n"
            f"📦 Volume: {int(volume):,} · Notional: ≈ ${notional:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 CHEAP — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 Underlying ${under_px:.2f} · 🎯 DTE {dte}\n"
            "────────────\n"
            f"{body}"
        )

        _mark_alerted_contract(contract)
        send_alert("cheap", sym, under_px, MIN_RVOL_GLOBAL, extra=extra)