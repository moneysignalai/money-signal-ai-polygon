# bots/cheap.py — Cheap 0–3 DTE options flow bot
#
# Hunts for:
#   • 0–3 DTE CALLs & PUTs
#   • Underlying in a reasonable price band (defaults: $3–$120)
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
MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "3.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "120.0"))

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
      - volume is contracts volume.
      - notional assumes last_price * volume * 100.
    """
    # Polygon option snapshot can have several price fields; we prefer last_trade_price.
    last_px = None

    # Try "last_quote" then "last_trade" then direct "price"
    last_trade = opt.get("last_trade") or {}
    last_quote = opt.get("last_quote") or {}

    # Priority: last trade price, then mid-quote, then bid/ask, then price
    if "price" in last_trade and last_trade.get("price") is not None:
        last_px = float(last_trade["price"])
    else:
        bid = last_quote.get("bid")
        ask = last_quote.get("ask")
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            last_px = (float(bid) + float(ask)) / 2.0
        elif bid is not None and bid > 0:
            last_px = float(bid)
        elif ask is not None and ask > 0:
            last_px = float(ask)
        elif "price" in opt and opt.get("price") is not None:
            last_px = float(opt["price"])

    # Volume
    volume = float(opt.get("day", {}).get("volume") or opt.get("volume") or 0.0)
    notional = 0.0
    if last_px is not None:
        notional = last_px * volume * 100.0

    return last_px, volume, notional


def _within_underlying_band(price: float) -> bool:
    return MIN_UNDERLYING_PRICE <= price <= MAX_UNDERLYING_PRICE


def _within_option_filters(
    dte: int,
    last_price: Optional[float],
    volume: float,
    notional: float,
) -> bool:
    if dte < 0 or dte > MAX_CHEAP_DTE:
        return False
    if last_price is None or last_price <= 0:
        return False
    if last_price > MAX_OPTION_PRICE:
        return False
    if volume < MIN_OPTION_VOLUME:
        return False
    if notional < MIN_OPTION_NOTIONAL:
        return False
    return True


def _nearest_strike(opt: Dict[str, Any]) -> Optional[float]:
    try:
        details = opt.get("details") or {}
        strike = details.get("strike_price") or opt.get("strike_price")
        if strike is None:
            return None
        return float(strike)
    except Exception:
        return None


def _option_type_label(opt: Dict[str, Any]) -> str:
    details = opt.get("details") or {}
    t = details.get("contract_type") or opt.get("contract_type") or ""
    t = str(t).lower()
    if t == "call":
        return "CALL"
    if t == "put":
        return "PUT"
    return "OPT"


def _distance_from_money(under_px: float, strike: Optional[float]) -> Tuple[str, float]:
    """
    Returns:
      label: "ATM / ITM / OTM"
      dist_pct: abs(strike - underlying) / underlying * 100
    """
    if strike is None or under_px <= 0:
        return "N/A", 0.0

    dist_pct = abs(strike - under_px) / under_px * 100.0

    if dist_pct < 1.0:
        label = "ATM"
    elif strike < under_px:
        label = "ITM"  # in-the-money for calls (approx)
    else:
        label = "OTM"
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
            under_px = _extract_underlying_price(opt)
            if under_px is None:
                continue

            if not _within_underlying_band(under_px):
                continue

            details = opt.get("details") or {}
            expiration = details.get("expiration_date")
            dte = _parse_dte(expiration, today)
            if dte is None:
                continue

            opt_type = _option_type_label(opt)
            if opt_type not in ("CALL", "PUT"):
                continue

            last_px, volume, notional = _extract_option_metrics(opt)
            if not _within_option_filters(dte, last_px, volume, notional):
                continue

            strike = _nearest_strike(opt)
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
                    "under_px": under_px,
                    "last_px": last_px,
                    "volume": volume,
                    "notional": notional,
                }

        if not best_candidate:
            continue

        contract = best_candidate["contract"]
        opt_type = best_candidate["opt_type"]
        dte = best_candidate["dte"]
        strike = best_candidate["strike"]
        under_px = best_candidate["under_px"]
        last_px = best_candidate["last_px"]
        volume = best_candidate["volume"]
        notional = best_candidate["notional"]

        m_label, m_dist = _distance_from_money(under_px, strike)

        moneyness_str = f"{m_label}"
        if m_dist > 0:
            moneyness_str += f" ({m_dist:.1f}% from spot)"

        body = (
            f"🎯 Cheap {opt_type} — `{contract}`\n"
            f"📌 Underlying: {sym} ≈ ${under_px:.2f}\n"
            f"🗓️ DTE: {dte} · Strike: {strike:.2f if strike is not None else 'N/A'}\n"
            f"📍 Moneyness: {moneyness_str}\n"
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