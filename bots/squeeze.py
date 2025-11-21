# bots/squeeze.py
#
# STOCK-BASED SQUEEZE BOT
# Looks for high-RVOL + strong % move + strong dollar volume breakouts.
#
# No options. This is strictly an EQUITY momentum bot.

import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from bots.shared import (
    POLYGON_KEY,
    get_last_trade_cached,
    get_dynamic_top_volume_universe,
    is_etf_blacklisted,
    chart_link,
    send_alert,
    now_est,
    is_rth,
    MIN_RVOL_GLOBAL,
)

import requests


# ---------------- CONFIG ----------------

# More aggressive defaults, but editable via env
MIN_RVOL = float(os.getenv("SQUEEZE_MIN_RVOL", "2.0"))
MIN_MOVE_PCT = float(os.getenv("SQUEEZE_MIN_MOVE", "4.0"))
MIN_DOLLAR_VOL = float(os.getenv("SQUEEZE_MIN_DOLLAR_VOL", "10000000"))   # $10M

MAX_UNIVERSE = int(os.getenv("SQUEEZE_MAX_UNIVERSE", "120"))

# Polygon endpoint for intraday 1-min bars
AGG_URL = "https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/minute/{from}/{to}"


def _safe_float(x):
    try:
        return float(x)
    except:
        return None


def fetch_intraday(sym: str) -> Optional[Dict[str, Any]]:
    """Fetch today's intraday 1-min bars for RVOL & move calculations."""
    if not POLYGON_KEY:
        print("[squeeze] missing POLYGON_KEY")
        return None

    today = datetime.utcnow().date()
    from_ = f"{today}T09:30:00Z"
    to_ = f"{today}T16:00:00Z"

    url = AGG_URL.format(sym=sym.upper(), from=from_, to=to_)
    params = {"apiKey": POLYGON_KEY}

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("results"):
            return data
    except Exception as e:
        print(f"[squeeze] intraday error for {sym}: {e}")

    return None


def compute_rvol(intraday_data: Dict[str, Any]) -> float:
    """Compute RVOL using today's volume vs average volume from bars."""
    results = intraday_data.get("results") or []
    if len(results) < 20:
        return 0.0

    todays_vol = sum(x.get("v", 0) for x in results)
    avg_bar_vol = sum(x.get("v", 0) for x in results[-20:]) / 20

    if avg_bar_vol <= 0:
        return 0.0

    return todays_vol / (avg_bar_vol * len(results))


async def run_squeeze() -> None:
    """Main STOCK squeeze scanner."""
    print("[squeeze] starting stock squeeze scan")

    if not is_rth():
        print("[squeeze] outside RTH, skipping")
        return

    universe = get_dynamic_top_volume_universe(max_tickers=MAX_UNIVERSE)
    if not universe:
        print("[squeeze] universe empty")
        return

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        last, approx_dollar_vol = get_last_trade_cached(sym)
        if not last or approx_dollar_vol is None:
            continue

        intraday = fetch_intraday(sym)
        if not intraday:
            continue

        bars = intraday.get("results") or []
        if not bars:
            continue

        # % move from open
        open_price = _safe_float(bars[0].get("o"))
        if not open_price or open_price <= 0:
            continue

        move_pct = ((last - open_price) / open_price) * 100

        # RVOL
        rvol = compute_rvol(intraday)

        # Dollar volume threshold
        if approx_dollar_vol < MIN_DOLLAR_VOL:
            continue

        # Screening
        if rvol < MIN_RVOL:
            continue
        if move_pct < MIN_MOVE_PCT:
            continue

        # Alert formatting
        extra = (
            f"🔥 STOCK SQUEEZE\n"
            f"🕒 {now_est()}\n"
            "────────────\n"
            f"📈 Move: {move_pct:.1f}%\n"
            f"📊 RVOL: {rvol:.1f}x\n"
            f"💵 Dollar Vol: ${approx_dollar_vol:,.0f}\n"
            "────────────\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        send_alert("squeeze", sym, last, rvol, extra=extra)

    print("[squeeze] scan complete")