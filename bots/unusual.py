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

MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))
MIN_OPTION_VOLUME = int(os.getenv("UNUSUAL_MIN_VOLUME", "500"))
MIN_OPTION_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "200000"))
MIN_UNDERLYING_PRICE = float(os.getenv("UNUSUAL_MIN_PRICE", "5.0"))

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
    U.S. options regular session:
      • Monday–Friday only
      • 9:30 AM – 4:00 PM Eastern
    """
    now = datetime.now(eastern)

    # 0 = Monday, 6 = Sunday
    if now.weekday() >= 5:
        # Saturday or Sunday
        print("[unusual] Weekend; skipping scan.")
        return False

    mins = now.hour * 60 + now.minute
    in_window = 9 * 60 + 30 <= mins <= 16 * 60

    if in_window:
        print("[unusual] Inside 09:30–16:00 RTH; scanning unusual sweeps.")
    else:
        print("[unusual] Outside 09:30–16:00 RTH; skipping scan.")

    return in_window


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    return get_dynamic_top_volume_universe(max_tickers=150, volume_coverage=0.95)


def _safe(o: Any, *names: str, default=None):
    for n in names:
        if hasattr(o, n):
            v = getattr(o, n)
            if v is not None:
                return v
    return default


# -----------------------------------------------------
#                MAIN BOT
# -----------------------------------------------------

async def run_unusual():
    """
    UNUSUAL OPTION SWEEPS — CALLS + PUTS
    Hybrid Format (Style C)
    """
    if not POLYGON_KEY or not _client:
        print("[unusual] Missing client or API key.")
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
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[unusual] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        d0 = days[-1]
        d1 = days[-2]

        last_price = float(d0.close)
        prev_close = float(d1.close)

        if last_price < MIN_UNDERLYING_PRICE:
            continue

        # compute RVOL
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = sum(d.volume for d in recent) / len(recent)
        else:
            avg_vol = d0.volume

        day_vol = float(d0.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        if rvol < MIN_RVOL_GLOBAL:
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

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
            print(f"[unusual] option fetch failed for {sym}: {e}")
            continue

        best = None  # biggest sweep

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
                except:
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

                moneyness = (last_price - strike) / last_price * 100.0

                data = {
                    "contract": c,
                    "ctype": ctype,
                    "mid": mid,
                    "bid": bid,
                    "ask": ask,
                    "vol": vol,
                    "notional": notional,
                    "dte": dte,
                    "exp": exp,
                    "strike": strike,
                    "moneyness": moneyness,
                }

                if not best or notional > best["notional"]:
                    best = data

            except Exception as ee:
                print(f"[unusual] error {sym}: {ee}")
                continue

        if not best:
            continue

        c = best["contract"]
        strike = best["strike"]
        ctype = best["ctype"].upper()  # CALL or PUT

        flow_emoji = "🕵️"
        money_emoji = "💰"
        clock_emoji = "🕒"
        divider = "────────────"

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        # simple sweep-type classification
        if best["bid"] == 0 or best["ask"] == 0:
            sweep_type = "Mixed flow"
        elif abs(best["mid"] - best["ask"]) < 1e-4:
            sweep_type = "Aggressive ASK buy"
        elif abs(best["mid"] - best["bid"]) < 1e-4:
            sweep_type = "Aggressive BID hit"
        else:
            sweep_type = "Directional sweep"

        itm_otm = (
            "ITM"
            if (ctype == "CALL" and last_price > strike)
            or (ctype == "PUT" and last_price < strike)
            else "OTM"
        )

        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        extra = (
            f"{flow_emoji} UNUSUAL — {sym}\n"
            f"{clock_emoji} {timestamp}\n"
            f"{money_emoji} ${last_price:.2f}\n"
            f"{divider}\n"
            f"{flow_emoji} Unusual {ctype} sweep: {sym} {best['exp']} {strike:.2f} {ctype[0]}\n"
            f"📌 Flow Type: {sweep_type}\n"
            f"⏱ DTE: {best['dte']} · {itm_otm} · Moneyness {best['moneyness']:.1f}%\n"
            f"📦 Volume: {best['vol']:,} contracts · Avg: ${best['mid']:.2f}\n"
            f"💰 Notional: ≈ ${best['notional']:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("unusual", sym, last_price, rvol, extra=extra)