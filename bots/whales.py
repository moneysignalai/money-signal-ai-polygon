# bots/whales.py — $2M+ whale orders (CALL + PUT) with option-cache

import os
from datetime import datetime
import pytz
from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
)

eastern = pytz.timezone("US/Eastern")

MIN_WHALE_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "2000000"))

async def run_whales():
    now = datetime.now(eastern)

    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 16:
        return  # RTH only

    universe = get_dynamic_top_volume_universe()

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        for c in chain.get("results", []):
            full = c.get("ticker")
            if not full:
                continue

            trade = get_last_option_trades_cached(full)
            if not trade:
                continue

            tr = trade.get("results", {})
            price = tr.get("price")
            size = tr.get("size", 0)

            if not price:
                continue

            notional = price * size * 100
            if notional < MIN_WHALE_NOTIONAL:
                continue

            cp = "CALL" if c.get("contract_type") == "call" else "PUT"

            msg = (
                f"🐋 *WHALE FLOW — {sym}*\n"
                f"🕒 {now.strftime('%I:%M %p EST').lstrip('0')} · {now.strftime('%b %d')}\n"
                f"💰 Underlying: {sym}\n"
                "────────────\n"
                f"🐋 {cp} WHALE: `{full}`\n"
                f"📦 Volume: {size:,}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("whales", sym, price, 0, extra=msg)