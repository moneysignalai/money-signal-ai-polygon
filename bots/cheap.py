# bots/cheap.py — WILL FIRE ALERTS TODAY
from .shared import client, send_alert, CHEAP_MAX_PRICE, CHEAP_MIN_RVOL, CHEAP_MIN_IV
from datetime import datetime, timedelta

async def run_cheap():
    try:
        contracts = client.list_options_contracts(
            contract_type="call",
            expiration_date_gte=datetime.now().strftime("%Y-%m-%d"),
            expiration_date_lte=(datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d"),
            limit=1000
        )
        seen = set()
        for c in contracts:
            t = c.underlying_ticker
            if not t or t in seen or t.startswith("^"): continue
            seen.add(t)
            try:
                bars = client.get_aggs(t, 1, "day", from_=(datetime.now()-timedelta(days=40)).date(), limit=40)
                if len(bars) < 10: continue
                df = __import__("pandas").DataFrame([b.__dict__ for b in bars])
                price = df["close"].iloc[-1]
                if price > CHEAP_MAX_PRICE: continue
                vol_today = df["volume"].iloc[-1]
                vol_avg = df["volume"].iloc[:-1].mean()
                rvol = round(vol_today / vol_avg, 2) if vol_avg > 0 else 0
                if rvol < CHEAP_MIN_RVOL: continue
                iv = c.implied_volatility or 0
                if iv < CHEAP_MIN_IV: continue
                extra = f"0–5 DTE CALL · IV {iv:.0%} · RVOL {rvol}x · Ask ${c.last_quote.ask:.2f}"
                await send_alert("cheap", t, price, rvol, extra)
            except: continue
    except Exception as e:
        print(f"Cheap bot error: {e}")
