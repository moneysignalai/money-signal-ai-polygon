import os
from typing import List, Tuple

import requests

from bots.shared import POLYGON_KEY, send_alert

# Minimum filters for premarket moves
MIN_PREMARKET_MOVE_PCT = float(os.getenv("MIN_PREMARKET_MOVE_PCT", "3.0"))      # 3% move
MIN_PREMARKET_VOLUME = int(os.getenv("MIN_PREMARKET_VOLUME", "100000"))         # 100k shares
MIN_PREMARKET_PRICE = float(os.getenv("MIN_PREMARKET_PRICE", "2.0"))            # $2+
MAX_PREMARKET_ALERTS = int(os.getenv("MAX_PREMARKET_ALERTS", "25"))             # top 25


async def run_premarket():
    """
    Premarket bot:
      - Uses Polygon snapshot endpoint
      - Looks for stocks with strong % move and volume
      - Sends top N alerts

    This is meant to run before/around the open, but will still work intraday.
    """
    if not POLYGON_KEY:
        print("[premarket] POLYGON_KEY not set; skipping scan.")
        return

    url = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"

    try:
        resp = requests.get(url, params={"apiKey": POLYGON_KEY}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        tickers = data.get("tickers", [])
    except Exception as e:
        print(f"[premarket] snapshot fetch failed: {e}")
        return

    candidates: List[Tuple[str, float, float, float]] = []

    for t in tickers:
        sym = t.get("ticker")
        if not sym:
            continue

        day = t.get("day") or {}
        last_trade = t.get("lastTrade") or {}

        # price
        last = day.get("c") or last_trade.get("p")
        if last is None:
            continue
        last = float(last)

        # volume and % change
        vol = float(day.get("v") or 0.0)
        change_pct = day.get("todaysChangePerc")
        if change_pct is None:
            continue
        change_pct = float(change_pct)

        # basic filters
        if last < MIN_PREMARKET_PRICE:
            continue
        if vol < MIN_PREMARKET_VOLUME:
            continue
        if abs(change_pct) < MIN_PREMARKET_MOVE_PCT:
            continue

        # We score by |move%| * volume to prioritize "real" movers
        score = abs(change_pct) * vol
        candidates.append((sym, last, change_pct, score))

    if not candidates:
        print("[premarket] No symbols matched filters.")
        return

    # sort by score desc, take top N
    candidates.sort(key=lambda x: x[3], reverse=True)
    top = candidates[:MAX_PREMARKET_ALERTS]

    for sym, last, change_pct, _score in top:
        direction = "gapping up" if change_pct > 0 else "gapping down"
        extra = (
            f"Premarket {direction} {change_pct:.1f}%\n"
            f"Price ${last:.2f} · Volume ≥ {MIN_PREMARKET_VOLUME:,} (snapshot)"
        )
        # RVOL not used here, pass 0.0
        send_alert("premarket", sym, last, 0.0, extra=extra)