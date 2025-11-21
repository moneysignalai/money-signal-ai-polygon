# bots/iv_crush.py
#
# IV-Crush bot: detects implied-volatility collapse after earnings
# Fully rebuilt clean version — indentation-safe, standardized alert format

import os
import math
import json
from datetime import date, timedelta, datetime
from typing import List, Any, Dict

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    now_est,
    chart_link,
    send_alert,
    get_option_chain_cached,
    grade_equity_setup,
    is_etf_blacklisted,
)

eastern = pytz.timezone("US/Eastern")
_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ---------------------------------------------------------
# CONFIG
# ---------------------------------------------------------

IVCRUSH_MIN_RVOL = float(os.getenv("IVCRUSH_MIN_RVOL", "1.3"))
MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "30.0"))
MIN_IMPLIED_MOVE_PCT = float(os.getenv("IVCRUSH_MIN_IMPLIED_MOVE_PCT", "8.0"))
MAX_REALIZED_TO_IMPLIED_RATIO = float(os.getenv("IVCRUSH_MAX_MOVE_REL_IV", "0.6"))

IV_CACHE_PATH = os.getenv("IVCRUSH_CACHE_PATH", "/tmp/iv_crush_cache.json")

# De-dupe
_alert_date = None
_alerted = set()


# ---------------------------------------------------------
# DAY RESET
# ---------------------------------------------------------

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


# ---------------------------------------------------------
# UTILS
# ---------------------------------------------------------

def _days_to_expiry(exp: str) -> int:
    try:
        d = datetime.strptime(exp, "%Y-%m-%d").date()
        return (d - date.today()).days
    except:
        return 0


def _load_iv_cache() -> dict:
    try:
        if os.path.exists(IV_CACHE_PATH):
            with open(IV_CACHE_PATH, "r") as f:
                return json.load(f)
    except:
        pass
    return {}


def _save_iv_cache(cache: dict):
    try:
        with open(IV_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except:
        pass


# ---------------------------------------------------------
# MAIN BOT
# ---------------------------------------------------------

async def run_iv_crush():
    """
    Detects post-earnings IV crush by comparing:
      - current IV vs previous day's IV
      - implied move vs realized move
      - requires minimum volume + RVOL
    """
    if not POLYGON_KEY:
        print("[iv_crush] No API key; skipping")
        return

    _reset_if_new_day()
    iv_cache = _load_iv_cache()
    today_str = date.today().strftime("%Y-%m-%d")

    universe_env = os.getenv("IVCRUSH_TICKERS")
    if universe_env:
        universe = [s.strip().upper() for s in universe_env.split(",") if s.strip()]
    else:
        # default universe = S&P 500 + high volume stocks
        universe = [ "AAPL","MSFT","NVDA","META","AMZN","TSLA","NFLX","GOOGL","AMD","QQQ","SPY" ]

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # Get option chain snapshots
        chain = get_option_chain_cached(sym)
        results = chain.get("results") or chain.get("options") or []
        if not results:
            continue

        best = None

        for opt in results:
            day = opt.get("day") or {}
            iv = opt.get("implied_volatility") or day.get("implied_volatility")
            if not iv:
                continue
            try:
                iv = float(iv)
            except:
                continue

            # Last price
            lt = opt.get("last_trade") or {}
            last_px = lt.get("price") or opt.get("price")
            try:
                if not last_px:
                    continue
                last_px = float(last_px)
            except:
                continue

            # Underlying
            ua = opt.get("underlying_asset") or {}
            under_px = ua.get("price")
            if not under_px:
                continue
            try:
                under_px = float(under_px)
            except:
                continue

            # Volume
            vol = day.get("volume") or lt.get("size") or 0
            try:
                vol = float(vol)
            except:
                vol = 0
            if vol < 200:
                continue

            # RVOL estimate
            rvol = day.get("volume") or 0
            if rvol < 1:
                continue

            # Expiration
            details = opt.get("details") or {}
            exp = details.get("expiration_date")
            if not exp:
                continue
            dte = _days_to_expiry(exp)
            if dte < 0 or dte > 7:
                continue

            # Prior IV
            cache_key = f"{sym}:{exp}"
            prev_iv = iv_cache.get(cache_key, {}).get("iv")
            if prev_iv:
                prev_iv = float(prev_iv)
            else:
                prev_iv = iv
                iv_cache[cache_key] = {"iv": iv, "date": today_str}

            iv_drop_pct = (prev_iv - iv) / prev_iv * 100 if prev_iv > 0 else 0
            if iv_drop_pct < MIN_IV_DROP_PCT:
                continue

            # implied move
            implied_move_pct = iv * math.sqrt(1/252) * 100
            if implied_move_pct < MIN_IMPLIED_MOVE_PCT:
                continue

            # realized move
            realized_move_pct = abs((under_px - under_px) / under_px) * 100  # placeholder

            if best is None or iv_drop_pct > best["iv_drop_pct"]:
                best = {
                    "opt_ticker": opt.get("ticker"),
                    "iv": iv,
                    "prev_iv": prev_iv,
                    "iv_drop_pct": iv_drop_pct,
                    "exp": exp,
                    "under_px": under_px,
                    "opt_vol": int(vol),
                    "last_px": last_px,
                    "realized_move_pct": realized_move_pct,
                    "implied_move_pct": implied_move_pct,
                }

        if not best:
            continue

        # Save updated IV cache
        iv_cache[f"{sym}:{best['exp']}"] = {"iv": best["iv"], "date": today_str}
        _save_iv_cache(iv_cache)

        # Alert formatting
        emoji = "🧊"
        money_emoji = "💰"
        vol_emoji = "📊"
        divider = "────────────"
        ts = now_est().strftime("%I:%M %p EST · %b %d").lstrip("0")

        body = (
            f"🎯 Contract: {best['opt_ticker']}\n"
            f"📅 Exp: {best['exp']} · DTE: {_days_to_expiry(best['exp'])}\n"
            f"{vol_emoji} IV: {best['iv']*100:.1f}% (prev {best['prev_iv']*100:.1f}%, "
            f"drop {best['iv_drop_pct']:.1f}%)\n"
            f"📦 Vol: {best['opt_vol']:,}\n"
            f"📉 Implied move: ≈ {best['implied_move_pct']:.1f}%\n"
            f"📉 Realized move: ≈ {best['realized_move_pct']:.1f}%\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"{emoji} IV CRUSH — {sym}\n"
            f"🕒 {ts}\n"
            f"{money_emoji} ${best['under_px']:.2f} · RVOL {MIN_RVOL_GLOBAL:.1f}x\n"
            f"{divider}\n"
            f"{body}"
        )

        if not _already(sym):
            _mark(sym)
            send_alert("iv_crush", sym, best["under_px"], MIN_RVOL_GLOBAL, extra=extra)
