# bots/iv_crush.py

import os
from datetime import date, timedelta, datetime
from typing import List, Any
import math
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
    grade_equity_setup,
    is_etf_blacklisted,
    chart_link,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

MIN_PRICE = float(os.getenv("IVCRUSH_MIN_PRICE", "5.0"))
MIN_RVOL = float(os.getenv("IVCRUSH_MIN_RVOL", "1.5"))
MIN_DOLLAR_VOL = float(os.getenv("IVCRUSH_MIN_DOLLAR_VOL", "10000000"))  # $10M

MAX_DTE_IV = int(os.getenv("IVCRUSH_MAX_DTE", "30"))
MIN_DTE_IV = int(os.getenv("IVCRUSH_MIN_DTE", "5"))

MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "30.0"))   # 30%+
MAX_MOVE_REL_TO_IV = float(os.getenv("IVCRUSH_MAX_MOVE_REL_IV", "0.7"))  # actual move <= 70% of implied

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


def _in_iv_window() -> bool:
    """
    IV crush is most interesting after earnings or big events.
    We scan 7:00–16:00 ET.
    """
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 7 * 60 <= mins <= 16 * 60


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


async def run_iv_crush():
    """
    IV Crush / Earnings Post-Mortem Bot:

      • Underlying:
          - Price >= MIN_PRICE
          - RVOL >= max(MIN_RVOL, MIN_RVOL_GLOBAL)
          - Dollar volume >= MIN_DOLLAR_VOL
      • Options:
          - Focus on near-the-money, short-dated (MIN_DTE_IV–MAX_DTE_IV).
          - IV drop >= MIN_IV_DROP_PCT.
          - Actual move smaller than the implied move (IV-based).
    """
    if not POLYGON_KEY or not _client:
        print("[iv_crush] Missing client/API key.")
        return
    if not _in_iv_window():
        print("[iv_crush] Outside IV crush window; skipping.")
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

        # --- Daily context ---

        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=30)).isoformat(),
                    to=today_s,
                    limit=30,
                )
            )
        except Exception as e:
            print(f"[iv_crush] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        d0 = days[-1]
        d1 = days[-2]

        last_price = float(d0.close)
        prev_close = float(d1.close)

        if last_price < MIN_PRICE:
            continue

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        hist = days[:-1]
        recent = hist[-20:] if len(hist) > 20 else hist
        avg_vol = sum(d.volume for d in recent) / len(recent)
        day_vol = float(d0.volume)
        rvol = day_vol / avg_vol if avg_vol > 0 else 1.0

        if rvol < max(MIN_RVOL, MIN_RVOL_GLOBAL):
            continue
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        dollar_vol = last_price * day_vol
        if dollar_vol < MIN_DOLLAR_VOL:
            continue

        # --- Look at short-dated, near-the-money options for IV crush ---

        try:
            contracts = list(
                _client.list_options_contracts(
                    underlying_ticker=sym,
                    expiration_date_gte=today_s,
                    expiration_date_lte=(today + timedelta(days=MAX_DTE_IV)).isoformat(),
                    limit=300,
                )
            )
        except Exception as e:
            print(f"[iv_crush] options fetch failed for {sym}: {e}")
            continue

        best = None

        for c in contracts:
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
                if dte < MIN_DTE_IV or dte > MAX_DTE_IV:
                    continue

                strike = float(_safe(c, "strike_price", "strike", default=0.0) or 0.0)
                if strike <= 0:
                    continue

                # focus near-the-money
                moneyness_pct = abs(last_price - strike) / last_price * 100.0
                if moneyness_pct > 10.0:
                    # too far OTM/ITM for core IV crush read
                    continue

                iv = float(_safe(c, "implied_volatility", "iv", default=0.0) or 0.0)
                prev_iv = float(_safe(c, "prev_day_implied_volatility", "prev_iv", default=0.0) or 0.0)

                if iv <= 0 or prev_iv <= 0:
                    # missing real IV data; skip
                    continue

                iv_drop_pct = (prev_iv - iv) / prev_iv * 100.0

                if iv_drop_pct < MIN_IV_DROP_PCT:
                    continue

                # approximate expected move from previous IV
                iv_annual = prev_iv / 100.0
                expected_move_pct = iv_annual * math.sqrt(dte / 365.0) * 100.0

                actual_move_abs = abs(move_pct)

                # Only consider if actual move is smaller than priced-in move
                if actual_move_abs > expected_move_pct * MAX_MOVE_REL_TO_IV:
                    # The move actually exceeded or matched implied; not classic IV crush edge
                    continue

                # choose most extreme IV crush
                data = {
                    "contract": c,
                    "ctype": ctype,
                    "strike": strike,
                    "dte": dte,
                    "exp": exp,
                    "iv": iv,
                    "prev_iv": prev_iv,
                    "iv_drop_pct": iv_drop_pct,
                    "expected_move_pct": expected_move_pct,
                    "actual_move_pct": move_pct,
                    "moneyness": moneyness_pct,
                }

                if not best or iv_drop_pct > best["iv_drop_pct"]:
                    best = data

            except Exception as ee:
                print(f"[iv_crush] error {sym}: {ee}")
                continue

        if not best:
            continue

        grade = grade_equity_setup(
            abs(best["actual_move_pct"]),
            rvol,
            dollar_vol,
        )

        emoji = "📉"
        vol_emoji = "📊"
        money_emoji = "💰"
        divider = "────────────"
        now_et = datetime.now(eastern)
        timestamp = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        ctype_up = best["ctype"].upper()
        extra = (
            f"{emoji} IV CRUSH — {sym}\n"
            f"🕒 {timestamp}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"{vol_emoji} {ctype_up} {sym} {best['exp']} {best['strike']:.2f} {ctype_up[0]}\n"
            f"📉 IV dropped {best['iv_drop_pct']:.1f}% (from {best['prev_iv']:.1f}% to {best['iv']:.1f}%)\n"
            f"🎯 Expected move (from IV): ≈ {best['expected_move_pct']:.1f}%\n"
            f"📊 Actual move: {best['actual_move_pct']:.1f}% (smaller than priced-in)\n"
            f"📍 Moneyness: {best['moneyness']:.1f}% from spot\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Edge: POST-EARNINGS VOL CRUSH\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("iv_crush", sym, last_price, rvol, extra=extra)