# bots/whales.py

import os
from datetime import date, timedelta, datetime
from typing import List, Any
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
    chart_link,
    is_etf_blacklisted,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ------------------- CONFIG -------------------

MAX_DTE = int(os.getenv("WHALES_MAX_DTE", "45"))  # focus nearer term
MIN_OPTION_VOLUME = int(os.getenv("WHALES_MIN_VOLUME", "1500"))
MIN_OPTION_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "2000000"))  # $2M+
MIN_UNDERLYING_PRICE = float(os.getenv("WHALES_MIN_PRICE", "10.0"))
MAX_UNDERLYING_PRICE = float(os.getenv("WHALES_MAX_PRICE", "500.0"))
MIN_DOLLAR_VOL = float(os.getenv("WHALES_MIN_DOLLAR_VOL", "50000000"))  # $50M
MIN_WHALES_RVOL = float(os.getenv("WHALES_MIN_RVOL", "3.0"))  # “oh wow” only

_alert_date: date | None = None
_alerted: set[str] = set()


def _reset_if_new_day():
    global _alert_date, _alerted
    today = date.today()
    if _alert_date != today:
        _alert_date = today
        _alerted = set()


def _already(sym: str) -> bool:
    _reset_if_new_day()
    return sym in _alerted


def _mark(sym: str):
    _reset_if_new_day()
    _alerted.add(sym)


def _in_rth() -> bool:
    """
    Mon–Fri, 9:30–16:00 ET (core options session).
    """
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        print("[whales] Weekend; skipping.")
        return False

    mins = now.hour * 60 + now.minute
    in_window = 9 * 60 + 30 <= mins <= 16 * 60
    if in_window:
        print("[whales] Inside RTH; scanning whales.")
    else:
        print("[whales] Outside RTH; skipping.")
    return in_window


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.97)


def _safe(o: Any, *names: str, default=None):
    for n in names:
        if hasattr(o, n):
            v = getattr(o, n)
            if v is not None:
                return v
    return default


async def run_whales():
    """
    WHALES BOT — Million-dollar+ options flow (CALLS + PUTS), “oh wow” only.

    Underlying filters:
      • Price ∈ [MIN_UNDERLYING_PRICE, MAX_UNDERLYING_PRICE]
      • RVOL ≥ max(MIN_WHALES_RVOL, MIN_RVOL_GLOBAL)
      • Volume ≥ MIN_VOLUME_GLOBAL
      • Dollar volume ≥ MIN_DOLLAR_VOL

    Option filters:
      • CALL or PUT
      • 0 < DTE ≤ MAX_DTE
      • Volume ≥ MIN_OPTION_VOLUME
      • Notional ≥ MIN_OPTION_NOTIONAL

    One largest whale per symbol per day.
    """
    if not POLYGON_KEY or not _client:
        print("[whales] Missing client/API key.")
        return
    if not _in_rth():
        return

    _reset_if_new_day()
    universe = _universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        # ---------- DAILY CONTEXT ----------
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=60)).isoformat(),
                    to=today_s,
                    limit=60,
                )
            )
        except Exception as e:
            print(f"[whales] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        d0 = days[-1]
        d1 = days[-2]

        last_price = float(d0.close)
        prev_close = float(d1.close)

        if last_price < MIN_UNDERLYING_PRICE or last_price > MAX_UNDERLYING_PRICE:
            continue

        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = sum(d.volume for d in recent) / len(recent)
        else:
            avg_vol = d0.volume

        day_vol = float(d0.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        dollar_vol = last_price * day_vol

        if rvol < max(MIN_WHALES_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue
        if dollar_vol < MIN_DOLLAR_VOL:
            continue

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        # ---------- OPTIONS SCAN ----------
        try:
            opts = list(
                _client.list_options_contracts(
                    underlying_ticker=sym,
                    expiration_date_gte=today_s,
                    expiration_date_lte=(today + timedelta(days=MAX_DTE)).isoformat(),
                    limit=800,
                )
            )
        except Exception as e:
            print(f"[whales] options fetch failed for {sym}: {e}")
            continue

        best = None  # biggest notional whale

        for c in opts:
            try:
                ctype = (_safe(c, "contract_type", "type", default="") or "").lower()
                if ctype not in ["call", "put"]:
                    continue

                exp = str(_safe(c, "expiration_date", default=""))[:10]
                if not exp:
                    continue
                try:
                    exp_dt = date.fromisoformat(exp)
                except Exception:
                    continue

                dte = (exp_dt - today).days
                if dte < 0 or dte > MAX_DTE:
                    continue

                q = _safe(c, "last_quote", default=None)
                if not q:
                    continue

                bid = float(_safe(q, "bid_price", "bid", default=0.0) or 0.0)
                ask = float(_safe(q, "ask_price", "ask", default=0.0) or 0.0)
                if bid <= 0 and ask <= 0:
                    continue

                mid = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
                if mid <= 0:
                    continue

                vol = int(_safe(c, "volume", default=0) or 0)
                if vol < MIN_OPTION_VOLUME:
                    continue

                notional = mid * vol * 100.0
                if notional < MIN_OPTION_NOTIONAL:
                    continue

                strike = float(_safe(c, "strike_price", "strike", default=0.0) or 0.0)
                if strike <= 0:
                    continue

                data = {
                    "ctype": ctype,
                    "mid": mid,
                    "vol": vol,
                    "notional": notional,
                    "dte": dte,
                    "exp": exp,
                    "strike": strike,
                }

                if not best or notional > best["notional"]:
                    best = data

            except Exception as ee:
                print(f"[whales] error {sym}: {ee}")
                continue

        if not best:
            continue

        ctype = best["ctype"].upper()
        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        direction_emoji = "🟢" if ctype == "CALL" else "🔻"
        whale_emoji = "🐋"
        money_emoji = "💰"
        clock_emoji = "🕒"
        divider = "────────────"

        extra = (
            f"{whale_emoji} *WHALE FLOW* — {sym}\n"
            f"{clock_emoji} {timestamp}\n"
            f"{money_emoji} Underlying: ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{direction_emoji} {sym} {best['exp']} {best['strike']:.2f} {ctype[0]}\n"
            f"📦 Volume: {best['vol']:,} · Avg: ${best['mid']:.2f}\n"
            f"💰 Notional: ≈ ${best['notional']:,.0f}\n"
            f"📊 Day Move: {move_pct:.1f}% · Dollar Vol ≈ ${dollar_vol:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("whales", sym, last_price, rvol, extra=extra)