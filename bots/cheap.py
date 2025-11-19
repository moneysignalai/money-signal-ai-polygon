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
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ---------------- CONFIG (tunable via ENV) ----------------

MIN_UNDERLYING_PRICE = float(os.getenv("CHEAP_MIN_UNDERLYING_PRICE", "10.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("CHEAP_MAX_UNDERLYING_PRICE", "80.0"))

MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "1.00"))  # max mid price per contract
MIN_OPTION_VOLUME = int(os.getenv("CHEAP_MIN_OPTION_VOLUME", "200"))
MIN_OPTION_NOTIONAL = float(os.getenv("CHEAP_MIN_OPTION_NOTIONAL", "25000"))  # mid * vol * 100

MAX_DTE_DAYS = int(os.getenv("CHEAP_MAX_DTE_DAYS", "5"))  # 0–5 DTE window
MIN_CHEAP_RVOL = float(os.getenv("CHEAP_MIN_RVOL", "1.6"))  # underlying RVOL floor

MIN_IV = float(os.getenv("CHEAP_MIN_IV", "0.0"))  # allow turning IV filter back on if desired
MAX_CONTRACTS_PER_SYMBOL = int(os.getenv("CHEAP_MAX_CONTRACTS_PER_SYMBOL", "1"))


def _in_cheap_window() -> bool:
    """Cheap 0DTE Hunter runs regular hours 9:30–16:00 EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _safe_attr(obj: Any, *names: str, default=None):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


async def run_cheap():
    """
    Cheap 0DTE / 0–5 DTE Hunter:

      • Underlying in top-volume universe
      • Underlying price between MIN_UNDERLYING_PRICE–MAX_UNDERLYING_PRICE
      • Underlying RVOL >= max(MIN_CHEAP_RVOL, MIN_RVOL_GLOBAL)
      • Underlying day volume >= MIN_VOLUME_GLOBAL
      • Options:
          - Calls only
          - Expiration 0–MAX_DTE_DAYS days out
          - Mid price <= MAX_PREMIUM
          - Volume >= MIN_OPTION_VOLUME
          - Notional (mid * vol * 100) >= MIN_OPTION_NOTIONAL
          - IV >= MIN_IV (if MIN_IV > 0)
      • Sends up to MAX_CONTRACTS_PER_SYMBOL per underlying per scan
    """
    if not POLYGON_KEY:
        print("[cheap] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[cheap] Client not initialized; skipping scan.")
        return
    if not _in_cheap_window():
        print("[cheap] Outside 9:30–16:00 window; skipping scan.")
        return

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()
    expiry_from = today
    expiry_to = today + timedelta(days=MAX_DTE_DAYS)
    exp_from_s = expiry_from.isoformat()
    exp_to_s = expiry_to.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # ---- Underlying daily filters ----
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[cheap] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        prev_bar = days[-2]

        last_price = float(today_bar.close)
        if last_price < MIN_UNDERLYING_PRICE or last_price > MAX_UNDERLYING_PRICE:
            continue

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        day_vol = float(today_bar.volume)
        rvol = float(day_vol / avg_vol) if avg_vol > 0 else 1.0

        if rvol < max(MIN_CHEAP_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # ---- Options scan: 0–MAX_DTE_DAYS calls ----
        try:
            contracts = list(
                _client.list_options_contracts(
                    underlying_ticker=sym,
                    expiration_date_gte=exp_from_s,
                    expiration_date_lte=exp_to_s,
                    limit=500,
                )
            )
        except Exception as e:
            print(f"[cheap] options list failed for {sym}: {e}")
            continue

        candidates: List[Dict[str, Any]] = []

        for c in contracts:
            try:
                ctype = (_safe_attr(c, "contract_type", "type", default="") or "").lower()
                if ctype != "call":
                    continue

                exp_str = _safe_attr(c, "expiration_date", default=None)
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

                iv = _safe_attr(c, "implied_volatility", default=None)
                if MIN_IV > 0 and iv is not None and float(iv) < MIN_IV:
                    continue

                candidates.append(
                    {
                        "contract": c,
                        "mid": float(mid),
                        "bid": float(bid),
                        "ask": float(ask),
                        "vol": vol,
                        "notional": float(notional),
                        "dte": dte,
                        "exp_str": str(exp_str),
                    }
                )
            except Exception as inner_e:
                print(f"[cheap] error processing contract for {sym}: {inner_e}")
                continue

        if not candidates:
            continue

        # sort by notional desc so we highlight the most serious flow
        candidates.sort(key=lambda x: x["notional"], reverse=True)
        top_n = candidates[:MAX_CONTRACTS_PER_SYMBOL]

        for cinfo in top_n:
            c = cinfo["contract"]
            mid = cinfo["mid"]
            bid = cinfo["bid"]
            ask = cinfo["ask"]
            vol = cinfo["vol"]
            notional = cinfo["notional"]
            dte = cinfo["dte"]
            exp_str = cinfo["exp_str"]

            emoji = "🎯"
            extra = (
                f"{emoji} CHEAP {dte}D CALL\n"
                f"Underlying {sym} ≈ ${last_price:.2f} · RVOL {rvol:.1f}x\n"
                f"📅 Exp: {exp_str} ({dte} DTE)\n"
                f"💸 Premium ≈ ${mid:.2f} (Bid ${bid:.2f} / Ask ${ask:.2f})\n"
                f"📦 Opt Vol {vol:,} · Notional ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("cheap", sym, last_price, rvol, extra=extra)