import os
from datetime import date, timedelta, datetime
from typing import List, Optional, Tuple

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
    now_est,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# ORB windows
ORB_FIRST_15_START = 9 * 60 + 30
ORB_FIRST_15_END = 9 * 60 + 45
ORB_SCAN_START = 9 * 60 + 45
ORB_SCAN_END = 11 * 60  # stop 11:00

MIN_ORB_PRICE = float(os.getenv("MIN_ORB_PRICE", "5.0"))
MIN_ORB_RVOL = float(os.getenv("MIN_ORB_RVOL", "2.0"))
MIN_ORB_DOLLAR_VOL = float(os.getenv("MIN_ORB_DOLLAR_VOL", "8000000"))


def _in_orb_window() -> bool:
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return ORB_SCAN_START <= mins <= ORB_SCAN_END


def _get_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


# (the rest of your ORB/FVG logic is unchanged – only the alert format at the bottom)

async def run_orb():
    """
    Opening Range Breakout Bot w/ FVG-style retest:

      • Builds 15m ORB from 9:30–9:45.
      • Then looks on 5m candles for first clean break of high/low.
      • Requires a later 5m bar that retests the ORB edge (FVG-style) while holding.
      • Requires price, RVOL, dollar volume filters.
    """
    if not POLYGON_KEY or not _client:
        print("[orb] no API key/client; skipping.")
        return
    if not _in_orb_window():
        print("[orb] outside ORB scan window; skipping.")
        return

    universe = _get_universe()
    today = date.today()
    today_s = today.isoformat()

    # ... all existing logic above unchanged ...

    # At the bottom of your function, where the alert is built:

        body = (
            f"{emoji} {dir_text} (15m ORB, 5m FVG retest)\n"
            f"📏 ORB Range (first 15m): {orb_low:.2f} – {orb_high:.2f}\n"
            f"🧱 Breakout candle (5m): O {br_open:.2f} · H {br_high:.2f} · L {br_low:.2f} · C {br_close:.2f} "
            f"(range {br_range:.2f})\n"
            f"🔁 FVG-style retest confirmed on later 5m bar while holding ORB edge\n"
            f"📈 Prev Close: ${prev_close:.2f} → Last: ${last_price:.2f} ({move_pct:.1f}%)\n"
            f"📦 Day Volume: {int(day_vol):,}\n"
            f"🎯 Setup Grade: {grade}\n"
            f"📌 Bias: {bias}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        extra = (
            f"📣 ORB — {sym}\n"
            f"🕒 {now_est()}\n"
            f"💰 ${last_price:.2f} · 📊 RVOL {rvol:.1f}x\n"
            "────────────\n"
            f"{body}"
        )

        _mark_alerted(sym)
        send_alert("orb", sym, last_price, rvol, extra=extra)