# bots/unusual.py — CALL + PUT + option-cache + enhanced alerts

import os
from datetime import datetime
import pytz
from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
    MIN_RVOL_GLOBAL,
)

eastern = pytz.timezone("US/Eastern")

MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "150000"))

async def run_unusual():
    now = datetime.now(eastern)
    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 16:
        return  # only RTH
    
    universe = get_dynamic_top_volume_universe()

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        contracts = chain.get("results", [])
        for c in contracts:
            full = c.get("ticker")
            if not full:
                continue

            trade = get_last_option_trades_cached(full)
            if not trade:
                continue

            last_trade = trade.get("results", {})
            price = last_trade.get("price")
            size = last_trade.get("size", 0)
            if not price:
                continue

            notional = price * size * 100  # options multiplier

            if notional < MIN_NOTIONAL:
                continue

            # CALL or PUT
            cp = "CALL" if c.get("contract_type") == "call" else "PUT"

            msg = (
                f"🕵️ *UNUSUAL — {sym}*\n"
                f"🕒 {now.strftime('%I:%M %p EST').lstrip('0')} · {now.strftime('%b %d')}\n"
                f"💰 ${price:.2f}\n"
                "────────────\n"
                f"🕵️ Unusual {cp} sweep: `{full}`\n"
                f"📦 Volume: {size:,}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("unusual", sym, price, 0, extra=msg)