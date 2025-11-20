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
# Implementation note:
#   We previously tried to use Polygon's snapshot client; that caused
#   `SnapshotClient.get_snapshot_ticker() missing 1 required positional argument: 'ticker'`
#   errors when called via RESTClient. This version instead uses daily aggregates
#   to derive price, volume, and an approximate RVOL.

from __future__ import annotations

import os
from datetime import datetime, date, timedelta
from typing import Optional, List

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

# ---------------- CONFIG ----------------

# Underlying price band
MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "5.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "150.0"))

# Underlying minimum dollar volume
MIN_UNDERLYING_DOLLAR_VOL = float(os.getenv("CHEAP_MIN_UNDERLYING_DOLLAR_VOL", "30000000"))  # $30M+

# Max DTE (in calendar days)
CHEAP_MAX_DTE = int(os.getenv("CHEAP_MAX_DTE", "5"))

# Core cheap zone (≤ $1.00)
MAX_CHEAP_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "1.00"))
MIN_CHEAP_VOLUME = int(os.getenv("CHEAP_MIN_VOLUME", "100"))          # 100+ contracts
MIN_CHEAP_NOTIONAL = float(os.getenv("CHEAP_MIN_NOTIONAL", "5000"))   # $5k +

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


def _reset_alerts_if_new_day():
    global _alert_date, _alerted_contracts
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted_contracts = set()


def _already_alerted(contract: str) -> bool:
    _reset_alerts_if_new_day()
    return contract in _alerted_contracts


def _mark_alerted_contract(contract: str) -> None:
    _reset_alerts_if_new_day()
    _alerted_contracts.add(contract)


def _in_rth() -> bool:
    now = now_est()
    mins = now.hour * 60 + now.minute
    return CHEAP_START_MIN <= mins <= CHEAP_END_MIN


def _get_universe() -> List[str]:
    env = os.getenv("CHEAP_TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    # Reasonable default: top-volume universe
    return get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.95)


def _extract_underlying_from_aggs(sym: str) -> tuple[Optional[float], float, float]:
    """
    Use daily aggregates to approximate:
      • under_px   → today's close
      • day_vol    → today's volume
      • approx_rvol → today's volume / avg( recent volume )
    """
    if not _client:
        return None, 0.0, 0.0

    today = date.today()
    from_ = (today - timedelta(days=30)).isoformat()
    to_ = today.isoformat()

    try:
        bars = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=from_,
                to=to_,
                limit=30,
            )
        )
    except Exception as e:
        print(f"[cheap] daily aggs error for {sym}: {e}")
        return None, 0.0, 0.0

    if len(bars) < 5:
        return None, 0.0, 0.0

    today_bar = bars[-1]
    try:
        under_px = float(today_bar.close)
        day_vol = float(today_bar.volume or 0.0)
    except Exception:
        return None, 0.0, 0.0

    prev_vols = [float(b.volume or 0.0) for b in bars[-6:-1]]  # last 5 before today
    avg_vol = sum(prev_vols) / max(len(prev_vols), 1)
    approx_rvol = day_vol / avg_vol if avg_vol > 0 else 0.0

    return under_px, day_vol, approx_rvol


def _calc_dte(expiration_date: date) -> int:
    return (expiration_date - date.today()).days


# ---------------- MAIN BOT ----------------


async def run_cheap():
    """
    Cheap Options Bot (Mode B: ≤$1 always + $1–$2 only with big flow).

    Steps:
      1) Check RTH window (09:30–16:00 ET).
      2) Build universe (env CHEAP_TICKER_UNIVERSE or dynamic top volume).
      3) For each symbol:
           - Skip ETFs on blacklist.
           - Get daily aggs → underlying price, day volume, approximate RVOL.
           - Enforce underlying price + dollar volume gates.
           - Fetch options snapshot chain.
           - Apply cheap-zone filters and alert.
    """
    if not POLYGON_KEY or not _client:
        print("[cheap] Missing POLYGON_KEY or client; skipping.")
        return

    if not _in_rth():
        print("[cheap] Outside RTH; skipping.")
        return

    _reset_alerts_if_new_day()

    universe = _get_universe()
    if not universe:
        print("[cheap] empty universe; skipping.")
        return

    today = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Underlying context from daily aggregates
        under_px, day_vol, approx_rvol = _extract_underlying_from_aggs(sym)

        if under_px is None or under_px <= 0:
            continue
        if under_px < MIN_UNDERLYING_PRICE or under_px > MAX_UNDERLYING_PRICE:
            continue

        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = under_px * day_vol
        if dollar_vol < MIN_UNDERLYING_DOLLAR_VOL:
            continue

        # Fetch options snapshot chain for this underlying
        try:
            chain = _client.get_snapshot_option_chain(sym)
        except Exception as e:
            print(f"[cheap] error fetching option chain for {sym}: {e}")
            continue

        options = getattr(chain, "options", None) or getattr(chain, "results", None)
        if not options:
            continue

        for opt in options:
            # Expect either dict-like or object-like with symbol / details
            symbol = None
            if isinstance(opt, dict):
                symbol = opt.get("f_symbol") or opt.get("symbol") or opt.get("sym")
            else:
                symbol = getattr(opt, "f_symbol", None) or getattr(opt, "symbol", None)

            if not symbol:
                continue

            if _already_alerted(symbol):
                continue

            # Basic legs: CALL/PUT only
            otype = None
            if isinstance(opt, dict):
                otype = opt.get("type") or opt.get("o_type") or opt.get("option_type")
            else:
                otype = getattr(opt, "type", None) or getattr(opt, "option_type", None)

            if not otype:
                continue
            otype = str(otype).upper()
            if otype not in ("C", "CALL", "P", "PUT"):
                continue

            # DTE
            exp = None
            if isinstance(opt, dict):
                exp = opt.get("expiration_date") or opt.get("e")
            else:
                exp = getattr(opt, "expiration_date", None)

            if not exp:
                continue

            if isinstance(exp, str):
                try:
                    # Handle YYYY-MM-DD
                    exp_date = date.fromisoformat(exp[:10])
                except Exception:
                    continue
            elif isinstance(exp, date):
                exp_date = exp
            else:
                continue

            dte = _calc_dte(exp_date)
            if dte < 0 or dte > CHEAP_MAX_DTE:
                continue

            # Strike
            strike = None
            if isinstance(opt, dict):
                strike = opt.get("strike") or opt.get("k")
            else:
                strike = getattr(opt, "strike", None)
            try:
                strike_val = float(strike) if strike is not None else None
            except Exception:
                strike_val = None

            # Last option price (from last_quote)
            last_quote = None
            if isinstance(opt, dict):
                last_quote = opt.get("last_quote") or opt.get("lastQuote")
            else:
                last_quote = getattr(opt, "last_quote", None) or getattr(opt, "lastQuote", None)

            opt_price = None
            if isinstance(last_quote, dict):
                opt_price = (
                    last_quote.get("P")
                    or last_quote.get("p")
                    or last_quote.get("last")
                    or last_quote.get("price")
                )
            elif last_quote is not None:
                opt_price = getattr(last_quote, "P", None) or getattr(last_quote, "price", None)

            if opt_price is None:
                continue
            try:
                opt_price = float(opt_price)
            except Exception:
                continue

            # Volume (per contract)
            opt_volume = None
            if isinstance(opt, dict):
                opt_volume = opt.get("volume") or opt.get("v")
            else:
                opt_volume = getattr(opt, "volume", None) or getattr(opt, "v", None)

            if opt_volume is None:
                continue
            try:
                opt_volume = int(opt_volume)
            except Exception:
                continue

            notional = opt_price * opt_volume * 100  # contract size

            # ---- Cheap zone filters ----
            if opt_price <= MAX_CHEAP_PRICE:
                if opt_volume < MIN_CHEAP_VOLUME or notional < MIN_CHEAP_NOTIONAL:
                    continue
            elif opt_price <= 2.0:
                # Extended $1–$2 zone
                if opt_volume < MIN_EXT_VOLUME or notional < MIN_EXT_NOTIONAL:
                    continue
            else:
                # Above $2 — ignore
                continue

            # Build human text type
            side_label = "CALL" if otype in ("C", "CALL") else "PUT"

            # Timestamp
            ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

            divider = "────────────"
            money_emoji = "💰"
            emoji = "🎯"

            extra = (
                f"{emoji} CHEAP FLOW — {sym}\n"
                f"🕒 {ts}\n"
                f"{money_emoji} Underlying ${under_px:.2f} · 📊 RVOL ~{approx_rvol:.1f}x\n"
                f"{divider}\n"
                f"🧾 Contract: {symbol}\n"
                f"📌 Type: {side_label} · Strike: {strike_val if strike_val is not None else 'N/A'} · DTE: {dte}\n"
                f"📦 Volume: {opt_volume:,} · Last: ${opt_price:.2f}\n"
                f"💵 Notional (est): ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            _mark_alerted_contract(symbol)
            # We don't have a perfect RVOL here; we pass approx_rvol for display.
            send_alert("cheap", sym, under_px, approx_rvol, extra=extra)