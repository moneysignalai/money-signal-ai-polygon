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
# Alerts:
#   • One alert per contract per day.
#   • Premium Telegram format (emoji, timestamp, price, RVOL, divider, body, chart).

from __future__ import annotations

import os
from datetime import date, datetime
from typing import List, Optional

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
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
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "120.0"))
MIN_UNDERLYING_DOLLAR_VOL = float(os.getenv("CHEAP_MIN_UNDERLYING_DOLLAR_VOL", "5000000"))  # $5M+

# DTE window for options
CHEAP_MAX_DTE = int(os.getenv("CHEAP_MAX_DTE", "5"))  # 0–5 DTE by default

# Price bands (Mode B)
MAX_BASE_OPTION_PRICE = float(os.getenv("CHEAP_MAX_BASE_OPTION_PRICE", "1.00"))   # ≤ $1.00 = always eligible
MAX_EXT_OPTION_PRICE = float(os.getenv("CHEAP_MAX_EXT_OPTION_PRICE", "2.00"))     # (1.00, 2.00] = only big volume/notional

# Volume + notional thresholds
# Base zone (≤ $1.00)
MIN_BASE_VOLUME = int(os.getenv("CHEAP_MIN_BASE_VOLUME", "50"))         # 50+ contracts
MIN_BASE_NOTIONAL = float(os.getenv("CHEAP_MIN_BASE_NOTIONAL", "5000")) # $5k+

# Extended zone ($1.00–$2.00)
MIN_EXT_VOLUME = int(os.getenv("CHEAP_MIN_EXT_VOLUME", "200"))          # 200+ contracts
MIN_EXT_NOTIONAL = float(os.getenv("CHEAP_MIN_EXT_NOTIONAL", "20000"))  # $20k+

# Time window — only scan during RTH
CHEAP_START_MIN = 9 * 60 + 30   # 09:30
CHEAP_END_MIN = 16 * 60         # 16:00

# Daily de-dupe per contract
_alert_date: Optional[date] = None
_alerted_contracts: set[str] = set()


# ---------------- INTERNAL HELPERS ----------------


def _reset_if_new_day() -> None:
    global _alert_date, _alerted_contracts
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    return contract in _alerted_contracts


def _mark_alerted_contract(contract: str) -> None:
    _alerted_contracts.add(contract)


def _in_trading_window() -> bool:
    now_et = datetime.now(eastern)
    if now_et.weekday() >= 5:  # 0=Mon, 6=Sun
        return False
    mins = now_et.hour * 60 + now_et.minute
    return CHEAP_START_MIN <= mins <= CHEAP_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("CHEAP_TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _parse_dte(expiration_date: str) -> Optional[int]:
    try:
        dt = datetime.strptime(expiration_date, "%Y-%m-%d").date()
        return (dt - date.today()).days
    except Exception:
        return None


def _extract_underlying_price_and_volume(snapshot) -> tuple[Optional[float], float, float]:
    """
    From Polygon snapshot, extract:
      - last underlying price
      - day volume
      - estimated RVOL-like ratio (day_v / prev_day_v) — used only for display
    """
    under_px = None
    day_vol = 0.0
    rvol = 1.0

    # Last trade price
    last_trade = getattr(snapshot, "lastTrade", None)
    if isinstance(last_trade, dict):
        under_px = last_trade.get("p") or last_trade.get("price")
    elif hasattr(last_trade, "p"):
        under_px = last_trade.p

    if under_px is not None:
        try:
            under_px = float(under_px)
        except Exception:
            under_px = None

    # Day volume
    day = getattr(snapshot, "day", None)
    if isinstance(day, dict):
        day_vol = day.get("v") or day.get("volume") or 0
    elif hasattr(day, "v"):
        day_vol = day.v

    try:
        day_vol = float(day_vol or 0.0)
    except Exception:
        day_vol = 0.0

    # RVOL approximation: today's volume / yesterday's volume
    prev_day = getattr(snapshot, "prevDay", None)
    prev_vol = 0.0
    if isinstance(prev_day, dict):
        prev_vol = prev_day.get("v") or prev_day.get("volume") or 0
    elif hasattr(prev_day, "v"):
        prev_vol = prev_day.v

    try:
        prev_vol = float(prev_vol or 0.0)
    except Exception:
        prev_vol = 0.0

    if prev_vol > 0:
        rvol = day_vol / prev_vol
    else:
        rvol = 1.0

    return under_px, day_vol, rvol


def _describe_moneyness(under_px: Optional[float], strike: Optional[float], cp_label: str) -> str:
    if under_px is None or strike is None or under_px <= 0:
        return "N/A"

    dist_pct = abs(strike - under_px) / under_px * 100.0
    if cp_label == "CALL":
        if strike < under_px:
            ml = "ITM"
        elif dist_pct <= 1.0:
            ml = "ATM"
        else:
            ml = "OTM"
    else:
        if strike > under_px:
            ml = "ITM"
        elif dist_pct <= 1.0:
            ml = "ATM"
        else:
            ml = "OTM"

    return f"{ml} · {dist_pct:.1f}%"


def _option_passes_price_and_flow_filters(price: float, volume: float, notional: float) -> bool:
    """
    Mode B filters:
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

    # Extended zone: (1.00, 2.00]
    if price <= MAX_EXT_OPTION_PRICE:
        if volume < MIN_EXT_VOLUME:
            return False
        if notional < MIN_EXT_NOTIONAL:
            return False
        return True

    # Outside cheap range (>$2.00)
    return False


# ---------------- MAIN BOT ----------------


async def run_cheap():
    """
    Cheap Options Bot (Mode B: ≤$1 always + $1–$2 only with big flow).

    Steps:
      1) Check RTH window (09:30–16:00 ET).
      2) Build universe (env CHEAP_TICKER_UNIVERSE or dynamic top volume).
      3) For each symbol:
           - Skip ETFs on blacklist.
           - Get snapshot → underlying price, day volume, approximate RVOL.
           - Enforce underlying price + dollar volume gates.
      4) For each option on that underlying:
           - Short DTE (0–5 days by default).
           - Apply Mode B price/volume/notional logic.
           - One alert per contract per day.
    """
    _reset_if_new_day()

    if not _in_trading_window():
        print("[cheap] outside RTH window; skipping.")
        return

    if not POLYGON_KEY or not _client:
        print("[cheap] no POLYGON_KEY or client; skipping.")
        return

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
            snapshot = _client.get_snapshot_ticker(sym)
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

            dte = _parse_dte(expiry)
            if dte is None or dte < 0 or dte > CHEAP_MAX_DTE:
                continue

            # Contract type
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
                volume = 0.0

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
                f"🗓️ DTE: {dte_text} · Strike: ${strike_text}\n"
                f"📍 Moneyness: {moneyness}\n"
                f"💵 Option Price: ${opt_price:.2f}\n"
                f"📦 Volume: {int(volume):,} · Notional: ≈ ${notional:,.0f}\n"
                f"💰 Underlying: ${under_px:.2f} (≈ ${dollar_vol:,.0f} day notional)\n"
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