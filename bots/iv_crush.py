# bots/iv_crush.py

import os
import math
import json
from datetime import date, timedelta, datetime
from typing import List, Any, Dict

import pytz
import requests

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
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ------------- CONFIG (with ENV overrides) -------------

MIN_PRICE = float(os.getenv("IVCRUSH_MIN_PRICE", "5.0"))
MIN_RVOL = float(os.getenv("IVCRUSH_MIN_RVOL", "1.5"))
MIN_DOLLAR_VOL = float(os.getenv("IVCRUSH_MIN_DOLLAR_VOL", "10000000"))  # $10M

MAX_DTE = int(os.getenv("IVCRUSH_MAX_DTE", "14"))       # short-dated focus (2 weeks)
MIN_DTE = int(os.getenv("IVCRUSH_MIN_DTE", "3"))        # avoid pure 0DTE noise
MIN_IV = float(os.getenv("IVCRUSH_MIN_IV", "0.6"))      # 0.6 = 60% IV and up

MIN_OPTION_VOLUME = int(os.getenv("IVCRUSH_MIN_OPTION_VOLUME", "500"))
MIN_OPTION_OI = int(os.getenv("IVCRUSH_MIN_OPTION_OI", "200"))

# Real IV crush thresholds
MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "30.0"))   # ≥30% drop vs yesterday
MIN_IMPLIED_MOVE_PCT = float(os.getenv("IVCRUSH_MIN_IMPLIED_MOVE_PCT", "8.0"))  # at least 8% move priced in
MAX_REALIZED_TO_IMPLIED_RATIO = float(os.getenv("IVCRUSH_MAX_MOVE_REL_IV", "0.6"))
# |realized move| <= 60% of implied move

# Cache for previous-day IV values
IV_CACHE_PATH = os.getenv("IVCRUSH_CACHE_PATH", "/tmp/iv_crush_cache.json")

_alert_date: date | None = None
_alerted: set[str] = set()


# ------------- SMALL HELPERS -------------

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
    IV Crush is most interesting around/after earnings.
    We'll scan 07:00–16:00 ET.
    """
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 7 * 60 <= mins <= 16 * 60


def _universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [x.strip().upper() for x in env.split(",") if x.strip()]
    # Use the same dynamic universe logic as other bots
    return get_dynamic_top_volume_universe(max_tickers=120, volume_coverage=0.95)


def _safe(o: Any, *names: str, default=None):
    for n in names:
        if isinstance(o, dict):
            for name in names:
                if name in o and o[name] is not None:
                    return o[name]
            return default
        else:
            for name in names:
                if hasattr(o, name):
                    v = getattr(o, name)
                    if v is not None:
                        return v
    return default


def _ema(values, period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1.0)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val


def _days_to_expiry(exp: str) -> int:
    try:
        d = date.fromisoformat(exp[:10])
        today = date.today()
        return (d - today).days
    except Exception:
        return 9999


def _estimate_implied_move_pct(iv: float, dte: int) -> float:
    """
    Convert annualized IV (0.8 = 80%) to implied % move over dte days.
    """
    if iv <= 0 or dte <= 0:
        return 0.0
    return iv * (dte / 365.0) ** 0.5 * 100.0


def _load_iv_cache() -> Dict[str, Dict[str, float]]:
    try:
        with open(IV_CACHE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_iv_cache(cache: Dict[str, Dict[str, float]]):
    try:
        with open(IV_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"[iv_crush] failed to write cache: {e}")


# ------------- CORE BOT LOGIC -------------

async def run_iv_crush():
    """
    IV CRUSH / EARNINGS POST-MORTEM BOT

    What it looks for:
      • Underlying:
          - Price >= MIN_PRICE
          - RVOL >= max(MIN_RVOL, MIN_RVOL_GLOBAL)
          - Dollar volume >= MIN_DOLLAR_VOL
      • Options (per underlying):
          - Short-dated (MIN_DTE–MAX_DTE)
          - IV >= MIN_IV
          - Volume >= MIN_OPTION_VOLUME, OI >= MIN_OPTION_OI
          - IV today is down >= MIN_IV_DROP_PCT vs previous cached IV (yesterday)
          - Implied move (from prior IV) >= MIN_IMPLIED_MOVE_PCT
          - Realized move today <= MAX_REALIZED_TO_IMPLIED_RATIO * implied move
      • One best contract per symbol (biggest IV drop × volume).
      • 1 alert max per symbol per day.
    """
    if not POLYGON_KEY or not _client:
        print("[iv_crush] Missing client/API key.")
        return
    if not _in_iv_window():
        print("[iv_crush] Outside IV window; skipping.")
        return

    _reset_if_new_day()
    universe = _universe()
    today = date.today()
    today_s = today.isoformat()
    today_str = today.isoformat()

    iv_cache = _load_iv_cache()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue
        if _already(sym):
            continue

        # ------------ DAILY CONTEXT (price, RVOL, volume) ------------
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today - timedelta(days=40)).isoformat(),
                    to=today_s,
                    limit=40,
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

        move_pct = (
            (last_price - prev_close) / prev_close * 100.0
            if prev_close > 0 else 0.0
        )

        # ------------ OPTIONS SNAPSHOT (Polygon v3 snapshot/options/{underlying}) ------------
        snap_url = f"https://api.polygon.io/v3/snapshot/options/{sym}"
        try:
            resp = requests.get(snap_url, params={"apiKey": POLYGON_KEY}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[iv_crush] snapshot fetch failed for {sym}: {e}")
            continue

        results = data.get("results") or []
        if not results:
            continue

        best = None  # best contract for this symbol

        for c in results:
            try:
                details = _safe(c, "details", default={}) or {}
                day = _safe(c, "day", default={}) or {}

                opt_ticker = details.get("ticker") or c.get("ticker")
                contract_type = details.get("contract_type")
                exp = details.get("expiration_date")
                strike = float(details.get("strike_price", 0.0) or 0.0)

                if not opt_ticker or not exp or contract_type not in ("call", "put"):
                    continue

                dte = _days_to_expiry(exp)
                if dte < MIN_DTE or dte > MAX_DTE:
                    continue

                iv = float(c.get("implied_volatility") or 0.0)
                if iv < MIN_IV:
                    continue

                opt_vol = int(day.get("volume") or 0)
                oi = int(c.get("open_interest") or 0)
                if opt_vol < MIN_OPTION_VOLUME or oi < MIN_OPTION_OI:
                    continue

                # Near-the-money preference: keep within ~15% of spot
                moneyness_pct = abs(last_price - strike) / last_price * 100.0
                if moneyness_pct > 15.0:
                    continue

                cache_key = opt_ticker
                prev_info = iv_cache.get(cache_key)

                # If we don't have previous-day IV, just store current and move on
                if not prev_info or prev_info.get("date") == today_str:
                    iv_cache[cache_key] = {"iv": iv, "date": today_str}
                    continue

                prev_iv = float(prev_info.get("iv") or 0.0)
                prev_date = prev_info.get("date")

                if prev_iv <= 0.0 or prev_date >= today_str:
                    # nothing useful to compare
                    iv_cache[cache_key] = {"iv": iv, "date": today_str}
                    continue

                iv_drop_pct = (prev_iv - iv) / prev_iv * 100.0
                if iv_drop_pct < MIN_IV_DROP_PCT:
                    # not a strong enough IV crush vs yesterday
                    iv_cache[cache_key] = {"iv": iv, "date": today_str}
                    continue

                implied_move_pct = _estimate_implied_move_pct(prev_iv, dte)
                if implied_move_pct < MIN_IMPLIED_MOVE_PCT:
                    iv_cache[cache_key] = {"iv": iv, "date": today_str}
                    continue

                realized_abs = abs(move_pct)
                ratio = realized_abs / implied_move_pct if implied_move_pct > 0 else 999.0

                if ratio > MAX_REALIZED_TO_IMPLIED_RATIO:
                    # the stock actually moved close to or more than what was priced in
                    iv_cache[cache_key] = {"iv": iv, "date": today_str}
                    continue

                # This is a valid IV crush candidate
                # Score it by a mix of IV drop and contract volume
                score = iv_drop_pct * max(1, opt_vol)

                candidate = {
                    "opt_ticker": opt_ticker,
                    "contract_type": contract_type,
                    "exp": exp,
                    "strike": strike,
                    "iv": iv,
                    "prev_iv": prev_iv,
                    "iv_drop_pct": iv_drop_pct,
                    "implied_move_pct": implied_move_pct,
                    "realized_move_pct": move_pct,
                    "moneyness_pct": moneyness_pct,
                    "opt_vol": opt_vol,
                    "oi": oi,
                    "score": score,
                }

                if not best or score > best["score"]:
                    best = candidate

                # Update cache to today's IV after evaluation
                iv_cache[cache_key] = {"iv": iv, "date": today_str}

            except Exception as ee:
                print(f"[iv_crush] error processing option for {sym}: {ee}")
                continue

        if not best:
            continue

        grade = grade_equity_setup(
            abs(best["realized_move_pct"]),
            rvol,
            dollar_vol,
        )

        emoji = "🧊"
        vol_emoji = "📊"
        money_emoji = "💰"
        divider = "────────────"
        now_et = datetime.now(eastern)
        ts = now_et.strftime("%I:%M %p EST · %b %d").lstrip("0")

        direction = "CALL" if best["contract_type"] == "call" else "PUT"

        extra = (
            f"{emoji} IV CRUSH — {sym}\n"
            f"🕒 {ts}\n"
            f"{money_emoji} ${last_price:.2f} · RVOL {rvol:.1f}x\n"
            f"{divider}\n"
            f"🎯 Contract: {best['opt_ticker']} ({direction})\n"
            f"📅 Exp: {best['exp']} · DTE: {_days_to_expiry(best['exp'])}\n"
            f"🎯 Strike: ${best['strike']:.2f} · Moneyness: {best['moneyness_pct']:.1f}%\n"
            f"{vol_emoji} IV: {best['iv']*100:.1f}% (prev {best['prev_iv']*100:.1f}%, "
            f"drop {best['iv_drop_pct']:.1f}%)\n"
            f"📦 Vol: {best['opt_vol']:,} · OI: {best['oi']:,}\n"
            f"📉 Implied move (prev IV): ≈ {best['implied_move_pct']:.1f}%\n"
            f"📉 Realized move today: ≈ {best['realized_move_pct']:.1f}%\n"
            f"⚖️ Realized / Implied: {abs(best['realized_move_pct'])/best['implied_move_pct']:.2f}x\n"
            f"💵 Dollar Volume: ≈ ${dollar_vol:,.0f}\n"
            f"🎯 Setup Grade: {grade} · Edge: POST-EARNINGS VOL CRUSH\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        _mark(sym)
        send_alert("iv_crush", sym, last_price, rvol, extra=extra)

    # Save IV cache after scanning all symbols
    _save_iv_cache(iv_cache)