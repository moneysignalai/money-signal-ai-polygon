# bots/cheap.py — Cheap 0–3 DTE options flow bot
#
# Hunts for:
#   • 0–3 DTE CALLs & PUTs
#   • Underlying in a reasonable price band (defaults: $3–$120)
#   • Cheap premium (defaults: <= $1.00)
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
    grade_equity_setup,
    chart_link,
    now_est,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ------------- CONFIG (with ENV overrides) -------------

MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "3.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "120.0"))
MAX_CHEAP_DTE = int(os.getenv("CHEAP_MAX_DTE", "3"))  # 0–3 DTE by default

MAX_OPTION_PRICE = float(os.getenv("CHEAP_MAX_OPTION_PRICE", "1.00"))  # premium <= $1.00
MIN_OPTION_VOLUME = float(os.getenv("CHEAP_MIN_OPTION_VOLUME", "100"))  # contracts
MIN_OPTION_NOTIONAL = float(os.getenv("CHEAP_MIN_OPTION_NOTIONAL", "5000"))  # $5k+

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
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _parse_dte(expiry: str) -> int | None:
    try:
        dt = datetime.strptime(expiry, "%Y-%m-%d").date()
        return (dt - date.today()).days
    except Exception:
        return None


def _filter_underlying(under_px: float) -> bool:
    return MIN_UNDERLYING_PRICE <= under_px <= MAX_UNDERLYING_PRICE


async def run_cheap():
    """
    Scan the universe for very cheap, short-dated contracts with real size/notional.
    """
    _reset_if_new_day()

    if not POLYGON_KEY or not _client:
        print("[cheap] no API key; skipping.")
        return

    if not _in_trading_window():
        print("[cheap] outside RTH window; skipping.")
        return

    universe = _get_universe()
    if not universe:
        print("[cheap] empty universe; skipping.")
        return

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        try:
            snapshot = _client.get_snapshot_ticker(sym)
        except Exception as e:
            print(f"[cheap] snapshot error for {sym}: {e}")
            continue

        under_px = getattr(snapshot, "lastTrade", None)
        if isinstance(under_px, dict):
            under_px = under_px.get("p")
        elif hasattr(under_px, "p"):
            under_px = under_px.p

        if under_px is None:
            continue

        try:
            under_px = float(under_px)
        except Exception:
            continue

        if not _filter_underlying(under_px):
            continue

        # Minute bars / RVOL gates (using shared MIN_RVOL_GLOBAL / MIN_VOLUME_GLOBAL)
        try:
            aggs = _client.list_aggs(
                sym,
                1,
                "minute",
                date.today().isoformat(),
                date.today().isoformat(),
                limit=500,
                sort="asc",
            )
            bars = list(aggs)
        except Exception as e:
            print(f"[cheap] agg error for {sym}: {e}")
            continue

        if not bars:
            continue

        total_volume = sum(getattr(b, "v", 0) for b in bars)
        if total_volume < MIN_VOLUME_GLOBAL:
            continue

        # Option chain
        try:
            chain = _client.list_options_ticker(symbol=sym, limit=1000)
            options = list(chain)
        except Exception as e:
            print(f"[cheap] option chain error for {sym}: {e}")
            continue

        for opt in options:
            contract = getattr(opt, "ticker", None)
            if not contract or _already_alerted(contract):
                continue

            expiry = getattr(opt, "expiration_date", None)
            dte = _parse_dte(expiry) if expiry else None
            if dte is None or dte < 0 or dte > MAX_CHEAP_DTE:
                continue

            last_px = getattr(opt, "last_quote", None)
            if isinstance(last_px, dict):
                last_px = last_px.get("P") or last_px.get("p")
            elif hasattr(last_px, "P"):
                last_px = last_px.P

            if last_px is None:
                continue

            try:
                last_px = float(last_px)
            except Exception:
                continue

            if last_px <= 0 or last_px > MAX_OPTION_PRICE:
                continue

            volume = getattr(opt, "day", None)
            if isinstance(volume, dict):
                volume = volume.get("v")
            elif hasattr(volume, "v"):
                volume = volume.v

            if volume is None:
                continue

            try:
                volume = float(volume)
            except Exception:
                continue

            notional = last_px * volume * 100.0
            if volume < MIN_OPTION_VOLUME:
                continue
            if notional < MIN_OPTION_NOTIONAL:
                continue

            strike = getattr(opt, "strike_price", None)
            try:
                strike = float(strike) if strike is not None else None
            except Exception:
                strike = None

            cp_type = getattr(opt, "contract_type", "").upper()
            cp_label = "CALL" if cp_type == "CALL" else "PUT"

            moneyness_str = "N/A"
            if under_px and strike:
                dist = abs(strike - under_px) / under_px * 100.0
                if cp_label == "CALL":
                    if strike < under_px:
                        ml = "ITM"
                    elif dist <= 1.0:
                        ml = "ATM"
                    else:
                        ml = "OTM"
                else:
                    if strike > under_px:
                        ml = "ITM"
                    elif dist <= 1.0:
                        ml = "ATM"
                    else:
                        ml = "OTM"
                moneyness_str = f"{ml} · {dist:.1f}%"

            body = (
                f"🎯 Contract: {contract} ({cp_label})\n"
                f"🗓️ DTE: {dte} · Strike: {strike:.2f if strike is not None else 'N/A'}\n"
                f"📍 Moneyness: {moneyness_str}\n"
                f"💵 Option Price: ${last_px:.2f} (≤ ${MAX_OPTION_PRICE:.2f} cheap filter)\n"
                f"📦 Volume: {int(volume):,} · Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")
extra = (
    f"📣 CHEAP — {sym}\n"
    f"🕒 {ts}\n"
    f"💰 Underlying ${under_px:.2f} · 🎯 DTE {dte}\n"
    "────────────\n"
    f"{body}"
)

            _mark_alerted_contract(contract)
            send_alert("cheap", sym, under_px, MIN_RVOL_GLOBAL, extra=extra)