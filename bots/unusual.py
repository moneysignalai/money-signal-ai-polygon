# bots/unusual.py — fixed, upgraded, premium-format unusual options sweeps

import os
from datetime import datetime, date
import pytz

from bots.shared import (
    get_dynamic_top_volume_universe,
    get_option_chain_cached,
    get_last_option_trades_cached,
    send_alert,
    chart_link,
)

eastern = pytz.timezone("US/Eastern")

# ---------------- ENV CONFIG ----------------
MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "150000"))  # $150k+
MIN_TRADE_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "50"))          # min 50 contracts
MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "30"))                  # avoid long-dated noise

# --------------- Deduping --------------------
_alert_date: date | None = None
_alerted_contracts: set[str] = set()

def _reset_day():
    global _alert_date, _alerted_contracts
    today = date.today()
    if today != _alert_date:
        _alert_date = today
        _alerted_contracts = set()

def _already(contract: str) -> bool:
    _reset_day()
    return contract in _alerted_contracts

def _mark(contract: str):
    _reset_day()
    _alerted_contracts.add(contract)

# ---------------- TIME WINDOW ----------------
def _in_rth() -> bool:
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    return (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16

# ---------------- MAIN -----------------------
async def run_unusual():
    if not _in_rth():
        return

    universe = get_dynamic_top_volume_universe()
    today = date.today()

    for sym in universe:
        chain = get_option_chain_cached(sym)
        if not chain:
            continue

        results = chain.get("results", [])
        if not results:
            continue

        for opt in results:
            details = opt.get("details") or {}
            contract = opt.get("ticker")
            if not contract:
                continue

            # daily dedupe
            if _already(contract):
                continue

            # CALL or PUT
            cp_raw = details.get("contract_type")
            if cp_raw not in ("call", "put"):
                continue
            cp = "CALL" if cp_raw == "call" else "PUT"

            # DTE filtering
            exp = details.get("expiration_date")
            if not exp:
                continue
            try:
                exp_d = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (exp_d - today).days
                if dte < 0 or dte > MAX_DTE:
                    continue
            except:
                continue

            # Last trade
            trade = get_last_option_trades_cached(contract)
            if not trade:
                continue

            last = trade.get("results") or {}
            price = last.get("p")
            size = last.get("s", 0)

            if price is None or price <= 0:
                continue
            if size < MIN_TRADE_SIZE:
                continue

            notional = price * size * 100
            if notional < MIN_NOTIONAL:
                continue

            # Format alert time
            now = datetime.now(eastern)
            now_str = now.strftime("%I:%M %p EST · %b %d").lstrip("0")

            # ---- BUILD PREMIUM ALERT ----
            msg = (
                f"🕵️ UNUSUAL — {sym}\n"
                f"🕒 {now_str}\n"
                f"💰 Last Price: ${price:.2f}\n"
                "────────────\n"
                f"🕵️ Unusual {cp} Sweep\n"
                f"📌 Contract: `{contract}`\n"
                f"📦 Size: {size:,}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🗓️ DTE: {dte}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            # send
            send_alert("unusual", sym, price, 0, extra=msg)
            _mark(contract)