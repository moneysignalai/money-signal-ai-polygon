# bots/cheap.py — Cheap & Fast-Moving Options (0–5 DTE)
#
# Logic (Mode B):
#   • Always alert on contracts priced ≤ $1.00 that meet minimum volume/notional.
#   • Also alert on contracts priced > $1.00 and ≤ $2.00 *only if*:
#       - Volume is large (default: ≥ 200 contracts)
#       - Notional is meaningful (default: ≥ $20,000)
#
# Underlying filters:
#   • Price between MIN_UNDERLYING_PRICE and MAX_UNDERLYING_PRICE
#   • Strong underlying dollar volume (MIN_UNDERLYING_DOLLAR_VOL)
#
# Option filters:
#   • DTE between 0 and CHEAP_MAX_DTE (inclusive, default 5 days)
#   • CALL + PUT
#
# Universe:
#   • ENV TICKER_UNIVERSE override
#   • Otherwise dynamic top-volume universe from shared.get_dynamic_top_volume_universe()
#
# Output:
#   • A-style alert formatting (consistent with other bots)

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import List, Tuple, Optional

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
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------- CONFIG (ENV OVERRIDES) ----------------

# Underlying filters
MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "3.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "200.0"))
MIN_UNDERLYING_DOLLAR_VOL = float(os.getenv("CHEAP_MIN_UNDERLYING_DOLLAR_VOL", "20000000"))  # $20M+

# Option filters
CHEAP_MAX_DTE = int(os.getenv("CHEAP_MAX_DTE", "5"))  # 0–5 days to expiry

# Mode B price/flow thresholds
MAX_BASE_OPTION_PRICE = float(os.getenv("CHEAP_MAX_BASE_PRICE", "1.00"))
MAX_EXT_OPTION_PRICE = float(os.getenv("CHEAP_MAX_EXT_PRICE", "2.00"))

MIN_BASE_VOLUME = float(os.getenv("CHEAP_MIN_BASE_VOLUME", "50"))       # contracts
MIN_BASE_NOTIONAL = float(os.getenv("CHEAP_MIN_BASE_NOTIONAL", "5000"))  # dollars

MIN_EXT_VOLUME = float(os.getenv("CHEAP_MIN_EXT_VOLUME", "200"))
MIN_EXT_NOTIONAL = float(os.getenv("CHEAP_MIN_EXT_NOTIONAL", "20000"))

# ---------------- STATE ----------------

_alert_date: Optional[date] = None
_alerted_contracts: set[str] = set()


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_contracts
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    _reset_if_new_day()
    return contract in _alerted_contracts


def _mark_alerted_contract(contract: str) -> None:
    _reset_if_new_day()
    _alerted_contracts.add(contract)


def _in_regular_hours() -> bool:
    now = now_est()
    mins = now.hour * 60 + now.minute
    # Allow from 09:30–16:00 ET
    return 9 * 60 + 30 <= mins <= 16 * 60


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    # Reasonable default: top-volume names, capture ~95% of volume, up to 200 tickers
    return get_dynamic_top_volume_universe(max_tickers=200, volume_coverage=0.95)


# ---------------- HELPERS ----------------


def _extract_underlying_price_and_volume(snapshot) -> Tuple[Optional[float], float, float]:
    """
    Given Polygon's snapshot object for a single ticker, extract:
      - last trade price
      - day volume
      - approximate RVOL (using day.v vs day.av if available)

    Works with both dict and client-typed response objects.
    """
    if snapshot is None:
        return None, 0.0, 1.0

    # Handle dict response
    if isinstance(snapshot, dict):
        day = snapshot.get("day") or {}
        last = snapshot.get("lastTrade") or {}
        price = last.get("p") or last.get("price")
        vol = day.get("v") or day.get("volume") or 0
        avg_vol = day.get("av") or day.get("avg_volume") or vol
    else:
        day = getattr(snapshot, "day", None)
        last = getattr(snapshot, "last_trade", None)
        price = getattr(last, "p", None) or getattr(last, "price", None)
        vol = getattr(day, "v", 0) if day is not None else 0
        avg_vol = getattr(day, "av", vol) if day is not None else vol

    try:
        px = float(price)
    except Exception:
        px = None

    try:
        vol_f = float(vol or 0.0)
    except Exception:
        vol_f = 0.0

    try:
        avg_v = float(avg_vol or 0.0)
    except Exception:
        avg_v = vol_f

    rvol = vol_f / avg_v if avg_v > 0 else 1.0

    return px, vol_f, rvol


def _option_passes_price_and_flow_filters(price: float, volume: float, notional: float) -> bool:
    """
    Mode B logic:

      - If price ≤ MAX_BASE_OPTION_PRICE:
          volume ≥ MIN_BASE_VOLUME and notional ≥ MIN_BASE_NOTIONAL
      - If MAX_BASE_OPTION_PRICE < price ≤ MAX_EXT_OPTION_PRICE:
          volume ≥ MIN_EXT_VOLUME and notional ≥ MIN_EXT_NOTIONAL
      - Else: reject.
    """
    if price <= 0:
        return False

    # Base cheap zone: ≤ $1.00
    if price <= MAX_BASE_OPTION_PRICE:
        if volume < MIN_BASE_VOLUME:
            return False
        if notional < MIN_BASE_NOTIONAL:
            return False
        return True

    # Extended cheap zone: > $1.00 and ≤ $2.00
    if price <= MAX_EXT_OPTION_PRICE:
        if volume < MIN_EXT_VOLUME:
            return False
        if notional < MIN_EXT_NOTIONAL:
            return False
        return True

    # Above $2: out of scope for this bot
    return False


def _describe_moneyness(under_px: float, strike: Optional[float], cp_label: str) -> str:
    if strike is None or under_px <= 0:
        return "N/A"

    if cp_label == "CALL":
        diff = (strike - under_px) / under_px * 100.0
    else:
        diff = (under_px - strike) / under_px * 100.0

    if abs(diff) < 2.0:
        return "near-the-money"
    if diff < 0:
        return f"in-the-money ({abs(diff):.1f}% ITM)"
    return f"out-of-the-money ({diff:.1f}% OTM)"


# ---------------- MAIN BOT ----------------


async def run_cheap() -> None:
    """
    Cheap Options Bot (Mode B):
      - Focus on 0–5 DTE cheap contracts with real flow.
      - Strong underlying filters (price band + dollar volume).
      - Both CALLs and PUTs.
    """
    if not POLYGON_KEY or not _client:
        print("[cheap] Missing Polygon API key/client; skipping.")
        return
    if not _in_regular_hours():
        print("[cheap] Outside RTH; skipping.")
        return

    _reset_if_new_day()

    universe = _get_universe()
    if not universe:
        print("[cheap] empty universe; skipping.")
        return

    today = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Snapshot for underlying price & volume
        try:
            # FIX: proper call signature for Polygon RESTClient
            snapshot = _client.get_snapshot_ticker(ticker=sym)
        except Exception as e:
            print(f"[cheap] snapshot error for {sym}: {e}")
            continue

        under_px, day_vol, approx_rvol = _extract_underlying_price_and_volume(snapshot)

        if under_px is None or under_px <= 0:
            continue
        if under_px < MIN_UNDERLYING_PRICE or under_px > MAX_UNDERLYING_PRICE:
            continue

        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = under_px * day_vol
        if dollar_vol < MIN_UNDERLYING_DOLLAR_VOL:
            continue

        # Pull options chain from Polygon
        try:
            options = list(_client.list_options_ticker(symbol=sym, limit=1000))
        except Exception as e:
            print(f"[cheap] option chain error for {sym}: {e}")
            continue

        if not options:
            continue

        for opt in options:
            contract = getattr(opt, "ticker", None)
            if not contract:
                continue
            if _already_alerted(contract):
                continue

            # DTE
            expiry = getattr(opt, "expiration_date", None)
            if not expiry:
                continue

            try:
                exp_date = (
                    expiry
                    if isinstance(expiry, date)
                    else date.fromisoformat(str(expiry)[:10])
                )
            except Exception:
                continue

            dte = (exp_date - today).days
            if dte < 0 or dte > CHEAP_MAX_DTE:
                continue

            # CALL / PUT
            cp_type = getattr(opt, "contract_type", "").upper()
            cp_label = "CALL" if cp_type == "CALL" else "PUT"

            # Strike
            strike = getattr(opt, "strike_price", None)
            try:
                strike_val = float(strike) if strike is not None else None
            except Exception:
                strike_val = None

            # Last option price (from last_quote)
            last_quote = getattr(opt, "last_quote", None)
            opt_price = None
            if isinstance(last_quote, dict):
                opt_price = last_quote.get("P") or last_quote.get("p") or last_quote.get("last")
            elif hasattr(last_quote, "P"):
                opt_price = last_quote.P

            if opt_price is None:
                continue

            try:
                opt_price = float(opt_price)
            except Exception:
                continue

            if opt_price <= 0:
                continue

            # Daily contract volume
            day_data = getattr(opt, "day", None)
            volume = 0.0
            if isinstance(day_data, dict):
                volume = day_data.get("v") or day_data.get("volume") or 0
            elif hasattr(day_data, "v"):
                volume = day_data.v

            try:
                volume = float(volume or 0.0)
            except Exception:
                continue

            if volume <= 0:
                continue

            notional = opt_price * volume * 100.0

            # Mode B filters based on price + flow
            if not _option_passes_price_and_flow_filters(opt_price, volume, notional):
                continue

            # All checks passed → build alert
            moneyness = _describe_moneyness(under_px, strike_val, cp_label)

            # Format nicely
            dte_text = f"{dte} day" if dte == 1 else f"{dte} days"
            strike_text = f"{strike_val:.2f}" if strike_val is not None else "N/A"

            body = (
                f"🎯 Contract: {contract} ({cp_label})\n"
                f"📈 Underlying: {sym} @ ${under_px:.2f}\n"
                f"💵 Option Price: ${opt_price:.2f} · Volume: {int(volume):,} · Notional ≈ ${notional:,.0f}\n"
                f"⏱ DTE: {dte_text} · Strike: {strike_text} · {moneyness}\n"
                f"📊 Underlying Vol: {int(day_vol):,} shares (≈ ${dollar_vol:,.0f})\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

            extra = (
                f"📣 CHEAP — {sym}\n"
                f"🕒 {ts}\n"
                f"💰 ${under_px:.2f} · 📊 RVOL ~{approx_rvol:.1f}x\n"
                "────────────\n"
                f"{body}"
            )

            _mark_alerted_contract(contract)
            # We don't have a perfect RVOL here; we pass approx_rvol for display.
            send_alert("cheap", sym, under_px, approx_rvol, extra=extra)