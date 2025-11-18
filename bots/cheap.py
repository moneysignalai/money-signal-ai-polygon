# bots/cheap.py — ELITE 2025 VERSION (4–12 high-conviction alerts/day)
from .shared import send_alert
from .helpers import client
from datetime import datetime, timedelta

async def run_cheap():
    today = datetime.now().date()
    # Scan 0–5 DTE calls only (0DTE + next week)
    contracts = client.list_options_contracts(
        contract_type="call",
        expiration_date_gte=today.strftime("%Y-%m-%d"),
        expiration_date_lte=(today + timedelta(days=5)).strftime("%Y-%m-%d"),
        limit=1000
    )

    seen = set()
    for c in contracts:
        ticker = c.underlying_ticker
        if not ticker or ticker in seen or ticker.startswith("^"):
            continue
        seen.add(ticker)

        try:
            # Daily bars for RVOL & price
            bars = client.get_aggs(ticker, 1, "day", from_=(today - timedelta(days=60)).date(), limit=60)
            if len(bars) < 20:
                continue
            df = __import__("pandas").DataFrame([b.__dict__ for b in bars])
            price = df["close"].iloc[-1]

            # ELITE FILTERS — ONLY THE BEST
            if not (10 <= price <= 80):                    # $10–$80 sweet spot
                continue
            if price < 15 and c.implied_volatility < 0.85: # sub-$15 needs huge IV
                continue
            if price >= 15 and c.implied_volatility < 0.55: # $15+ needs decent IV
                continue

            # RVOL must be strong
            avg_vol = df["volume"].iloc[:-1].mean()
            today_vol = df["volume"].iloc[-1]
            rvol = today_vol / avg_vol if avg_vol > 0 else 0
            if rvol < 1.7:                                  # minimum 1.7x RVOL
                continue

            # Volume must be real (not just 100k shares)
            if today_vol < 800_000:
                continue

            # Option must have decent liquidity
            if c.last_quote.ask > 3.0 or c.last_quote.bid < 0.30:
                continue
            if c.volume < 300 and c.open_interest < 1000:   # some real interest
                continue

            # Final killer filter: bid/ask spread < 25%
            if c.last_quote.bid > 0 and (c.last_quote.ask - c.last_quote.bid) / c.last_quote.bid > 0.25:
                continue

            # Build elite message
            dte = (datetime.strptime(c.expiration_date, "%Y-%m-%d").date() - today).days
            extra = (
                f"CHEAP {dte}DTE CALL · ${price:.2f}\n"
                f"IV {c.implied_volatility:.0%} · RVOL {rvol:.1f}x · Vol {today_vol:,.0f}\n"
                f"Premium ${c.last_quote.bid:.2f}–${c.last_quote.ask:.2f} · Spread {(c.last_quote.ask-c.last_quote.bid)/c.last_quote.bid:.0%}\n"
                f"Strike ${c.strike_price} · Vol {c.volume:,} · OI {c.open_interest:,}"
            )

            await send_alert("cheap", ticker, price, round(rvol, 1), extra)

        except:
            continue