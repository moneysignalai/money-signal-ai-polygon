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
MIN_RVOL = float(os.getenv("IVCRUSH_MIN_RVOL", "1.3"))
MIN_DOLLAR_VOL = float(os.getenv("IVCRUSH_MIN_DOLLAR_VOL", "7500000"))  # $7.5M+

MAX_DTE = int(os.getenv("IVCRUSH_MAX_DTE", "14"))       # short-dated focus (2 weeks)
MIN_DTE = int(os.getenv("IVCRUSH_MIN_DTE", "3"))        # avoid pure 0DTE noise
MIN_IV = float(os.getenv("IVCRUSH_MIN_IV", "0.6"))      # 0.6 = 60% IV and up

MIN_OPTION_VOLUME = int(os.getenv("IVCRUSH_MIN_OPTION_VOLUME", "300"))
MIN_OPTION_OI = int(os.getenv("IVCRUSH_MIN_OPTION_OI", "100"))
MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "30.0"))   # ≥30% drop vs yesterday
MIN_IMPLIED_MOVE_PCT = float(os.getenv("IVCRUSH_MIN_IMPLIED_MOVE_PCT", "8.0"))  # at least 8% move priced in
MAX_REALIZED_TO_IMPLIED_RATIO = float(os.getenv("IVCRUSH_MAX_MOVE_REL_IV", "0.6"))

# ... (keep the rest of your existing IV crush logic exactly the same) ...
# The important part we standardized earlier is the alert body:

# inside your main loop where `best` is chosen and `grade` computed:

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