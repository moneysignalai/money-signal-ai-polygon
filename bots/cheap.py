import os
from datetime import date, timedelta, datetime
from typing import List, Dict, Any

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

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG (tunable via ENV) ----------------

MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "10.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "80.0"))

MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "0.50"))  # <= $0.50
MIN_OPTION_VOLUME = int(os.getenv("CHEAP_MIN_OPTION_VOLUME", "500"))
MIN_OPTION_NOTIONAL = float(os.getenv("CHEAP_MIN_OPTION_NOTIONAL", "25000"))
MAX_DTE_DAYS = int(os.getenv("CHEAP_MAX_DTE_DAYS", "5"))

# enforce regular hours
def _in_cheap_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


def _safe_attr(obj: Any, *names: str, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


async def run_cheap():
    """
    Cheap 0–5 DTE Options Bot:

      • Underlying between MIN_UNDERLYING_PRICE and MAX_UNDERLYING_PRICE.
      • CALLs only (you already have Squeeze and Panic for downside).
      • Mid premium <= MAX_PREMIUM.
      • Volume and notional filters.
    """
    if not POLYGON_KEY or not _client:
        print("[cheap] no API key/client; skipping.")
        return
    if not _in_cheap_window():
        print("[cheap] outside 9:30–16:00; skipping.")
        return

    universe = _get_universe()
    today = date.today()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # underlying snapshot
        try:
            snap = _client.get_snapshot("stocks", sym)
        except Exception as e:
            print(f"[cheap] snapshot failed for {sym}: {e}")
            continue

        last_price = float(_safe_attr(snap, "last_quote", "last_trade", default=None).p or 0.0) if hasattr(_safe_attr(snap, "last_quote", "last_trade", default=None), "p") else float(
            getattr(_safe_attr(snap, "last_quote", "last_trade", default=None), "price", 0.0)
        )

        if last_price < MIN_UNDERLYING_PRICE or last_price > MAX_UNDERLYING_PRICE:
            continue

        day_vol = float(getattr(snap, "volume", 0.0) or 0.0)
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # we don't recompute RVOL here; we just pass through MIN_RVOL_GLOBAL as filter via volume if you want.

        # option chain
        try:
            chain = list(_client.list_options_contracts(underlying_ticker=sym, limit=1000))
        except Exception as e:
            print(f"[cheap] option chain failed for {sym}: {e}")
            continue

        best: Dict[str, Any] = {}

        for c in chain:
            if getattr(c, "option_type", getattr(c, "type", "C")) != "C":
                continue

            exp_str = getattr(c, "expiration_date", None) or getattr(c, "expiration", None)
            if not exp_str:
                continue
            try:
                exp_dt = date.fromisoformat(str(exp_str)[:10])
            except Exception:
                continue
            dte = (exp_dt - today).days
            if dte < 0 or dte > MAX_DTE_DAYS:
                continue

            q = _safe_attr(c, "last_quote", default=None)
            if not q:
                continue

            bid = _safe_attr(q, "bid_price", "bid", default=0.0) or 0.0
            ask = _safe_attr(q, "ask_price", "ask", default=0.0) or 0.0
            if bid <= 0 and ask <= 0:
                continue

            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
            else:
                mid = max(bid, ask)

            if mid <= 0 or mid > MAX_PREMIUM:
                continue

            vol = int(_safe_attr(c, "volume", default=0) or 0)
            notional = mid * vol * 100.0

            if vol < MIN_OPTION_VOLUME:
                continue
            if notional < MIN_OPTION_NOTIONAL:
                continue

            key = f"{dte}"
            if key not in best or notional > best[key]["notional"]:
                best[key] = {
                    "contract": c,
                    "mid": mid,
                    "bid": bid,
                    "ask": ask,
                    "vol": vol,
                    "notional": notional,
                    "dte": dte,
                    "exp_str": exp_str,
                }

        for dkey, cinfo in best.items():
            c = cinfo["contract"]
            mid = cinfo["mid"]
            bid = cinfo["bid"]
            ask = cinfo["ask"]
            vol = cinfo["vol"]
            notional = cinfo["notional"]
            dte = cinfo["dte"]
            exp_str = cinfo["exp_str"]

            body = (
                f"🎯 CHEAP {dte}D CALL\n"
                f"Underlying {sym} ≈ ${last_price:.2f}\n"
                f"📅 Exp: {exp_str} ({dte} DTE)\n"
                f"💸 Premium ≈ ${mid:.2f} (Bid ${bid:.2f} / Ask ${ask:.2f})\n"
                f"📦 Opt Vol {vol:,} · Notional ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            extra = (
                f"📣 CHEAP — {sym}\n"
                f"🕒 {now_est()}\n"
                f"💰 ${last_price:.2f} · 📊 RVOL {MIN_RVOL_GLOBAL:.1f}x\n"
                "────────────\n"
                f"{body}"
            )

            send_alert("cheap", sym, last_price, MIN_RVOL_GLOBAL, extra=extra)