# bots/iv_crush.py — TRUE OPTIONS IV-CRUSH SCANNER (2025 REBUILD)

import os
import math
import json
from datetime import date, timedelta, datetime
from typing import List, Dict, Any

import pytz

from bots.shared import (
    POLYGON_KEY,
    get_option_chain_cached,
    now_est,
    chart_link,
    send_alert,
    is_etf_blacklisted,
)

eastern = pytz.timezone("US/Eastern")

# OPTION API CLIENT NOT NEEDED — using snapshot cache only

# ---------------- CONFIG ----------------
MIN_OPT_VOL = int(os.getenv("IVCRUSH_MIN_OPT_VOL", "200"))        # min contract vol
MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "35"))
MIN_IMPLIED_MOVE_PCT = float(os.getenv("IVCRUSH_MIN_IMPLIED_MOVE_PCT", "7"))
MAX_DTE = int(os.getenv("IVCRUSH_MAX_DTE", "7"))                  # weeklies only

IV_CACHE_PATH = os.getenv("IVCRUSH_CACHE_PATH", "/tmp/iv_crush_cache.json")

_alert_date = None
_alerted: set[str] = set()


# ---------------- RESET ----------------
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


# ---------------- UTIL FUNCTIONS ----------------

def _days_to_expiry(exp: str) -> int:
    try:
        d = datetime.strptime(exp, "%Y-%m-%d").date()
        return (d - date.today()).days
    except:
        return 0


def _load_cache() -> dict:
    try:
        if os.path.exists(IV_CACHE_PATH):
            with open(IV_CACHE_PATH, "r") as f:
                return json.load(f)
    except:
        pass
    return {}


def _save_cache(cache: dict):
    try:
        with open(IV_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except:
        pass


# ---------------- MAIN BOT ----------------

async def run_iv_crush():
    if not POLYGON_KEY:
        print("[iv_crush] No API key; skipping")
        return

    _reset_if_new_day()
    cache = _load_cache()
    today_str = date.today().isoformat()

    # Universe (large caps + high-volume options names)
    env = os.getenv("IVCRUSH_TICKERS")
    if env:
        universe = [s.strip().upper() for s in env.split(",") if s.strip()]
    else:
        universe = [
            "AAPL","MSFT","NVDA","TSLA","META","GOOGL",
            "AMZN","AMD","NFLX","QQQ","SPY","SMCI","BABA",
            "COIN","PLTR","SHOP","UBER","ABNB","IBM"
        ]

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        opts = chain.get("results") or chain.get("options") or []
        if not opts:
            continue

        best = None

        for opt in opts:
            # underlying
            u = opt.get("underlying_asset") or {}
            under_px = u.get("price")
            if not under_px:
                continue
            try:
                under_px = float(under_px)
            except:
                continue

            # IV
            iv_raw = (
                opt.get("implied_volatility") or
                (opt.get("day") or {}).get("implied_volatility")
            )
            if not iv_raw:
                continue
            try:
                iv = float(iv_raw)
            except:
                continue

            # volume (option-specific)
            vol = (
                opt.get("day", {}).get("volume") or
                (opt.get("last_trade", {}).get("size")) or
                0
            )
            try:
                vol = int(vol)
            except:
                vol = 0
            if vol < MIN_OPT_VOL:
                continue

            # expiration
            details = opt.get("details") or {}
            exp = details.get("expiration_date")
            if not exp:
                continue

            dte = _days_to_expiry(exp)
            if dte < 0 or dte > MAX_DTE:
                continue  # IV crush only happens right after earnings

            # prior IV from cache
            cache_key = f"{sym}:{exp}"
            prev_iv = cache.get(cache_key, {}).get("iv")

            if prev_iv:
                prev_iv = float(prev_iv)
            else:
                prev_iv = iv
                cache[cache_key] = {"iv": iv, "date": today_str}
                continue  # need previous day's IV to measure the crush

            iv_drop_pct = (prev_iv - iv) / prev_iv * 100
            if iv_drop_pct < MIN_IV_DROP_PCT:
                continue

            # implied move scale
            implied_move_pct = iv * math.sqrt(1/252) * 100
            if implied_move_pct < MIN_IMPLIED_MOVE_PCT:
                continue

            # realized move = (open - current) / open
            open_px = u.get("day_open") or u.get("prev_close") or under_px
            try:
                open_px = float(open_px)
            except:
                open_px = under_px

            realized_move_pct = abs((under_px - open_px) / open_px * 100)

            # pick best (biggest IV crush)
            if best is None or iv_drop_pct > best["iv_drop_pct"]:
                best = {
                    "ticker": opt.get("ticker"),
                    "iv": iv,
                    "prev_iv": prev_iv,
                    "iv_drop_pct": iv_drop_pct,
                    "exp": exp,
                    "dte": dte,
                    "vol": vol,
                    "under_px": under_px,
                    "implied_move_pct": implied_move_pct,
                    "realized_move_pct": realized_move_pct,
                }

        if not best:
            continue

        # update cache
        cache[f"{sym}:{best['exp']}"] = {"iv": best["iv"], "date": today_str}
        _save_cache(cache)

        # alert body
        ts = now_est()
        divider = "────────────"

        extra = (
            f"🧊 IV CRUSH — {sym}\n"
            f"🕒 {ts}\n"
            f"💰 ${best['under_px']:.2f}\n"
            f"{divider}\n"
            f"🎯 Contract: `{best['ticker']}`\n"
            f"📅 Exp: {best['exp']} · DTE {best['dte']}\n"
            f"📉 IV: {best['iv']*100:.1f}% (prev {best['prev_iv']*100:.1f}%)\n"
            f"💥 Drop: {best['iv_drop_pct']:.1f}%\n"
            f"📦 Volume: {best['vol']:,}\n"
            f"📉 Implied Move: ≈ {best['implied_move_pct']:.1f}%\n"
            f"📉 Realized Move: ≈ {best['realized_move_pct']:.1f}%\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        if not _already(sym):
            _mark(sym)
            send_alert("iv_crush", sym, best["under_px"], 0, extra=extra)
