# bots/earnings.py — ELITE 2025 EARNINGS ROCKET DETECTOR (1–5 monsters per week)
import yfinance as yf
from datetime import datetime, timedelta
from .shared import send_alert, client
from .helpers import get_top_volume_stocks

async def run_earnings():
    # Only run during regular hours + after-hours if needed
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 7 or now.hour >= 22:
        return

    top_stocks = get_top_volume_stocks(150)

    for sym in top_stocks:
        try:
            ticker = yf.Ticker(sym)
            info = ticker.info
            cal = ticker.calendar
            if cal is None or cal.empty:
                continue

            # Next earnings date
            next_earnings = cal.iloc[0, 0]  # first column = date
            days_until = (next_earnings.date() - now.date()).days

            # Only 0–6 days away (includes today + next week)
            if not (0 <= days_until <= 6):
                continue

            # ELITE FILTERS
            price = client.get_last_trade(sym).price
            if not (8 <= price <= 200):                     # no pennies, no $500+ giants
                continue

            # Volume surge today (people loading up)
            bars = client.get_aggs(sym, 1, "day", limit=10)
            if len(bars) < 5:
                continue
            df = __import__("pandas").DataFrame([b.__dict__ for b in bars])
            avg_vol_5d = df["volume"].iloc[-6:-1].mean()
            today_vol = df["volume"].iloc[-1]
            rvol = today_vol / avg_vol_5d if avg_vol_5d > 0 else 0
            if rvol < 1.8:                                   # must be loading
                continue

            # Short interest bonus (earnings + short squeeze = nuclear)
            try:
                si = client.get_short_interest(sym, limit=1)[0]
                short_pct = si.short_interest / si.float_shares if si.float_shares else 0
            except:
                short_pct = 0

            # Implied move from options (must be big)
            exp = next_earnings.strftime("%Y-%m-%d")
            contracts = client.list_options_contracts(underlying_ticker=sym, expiration_date=exp, limit=100)
            if not contracts:
                continue
            atm_call = max([c for c in contracts if c.contract_type == "call"],
                           key=lambda x: abs(x.strike_price - price), default=None)
            if not atm_call or not atm_call.implied_volatility:
                continue
            implied_move = atm_call.implied_volatility * (days_until + 1) ** 0.5
            if implied_move < 0.09:  # less than 9% implied = boring
                continue

            # Final score
            score = min(100, int(
                rvol * 15 +
                implied_move * 100 +
                (30 if short_pct > 0.15 else 0) +
                (20 if days_until <= 1 else 0)  # today/tomorrow = extra points
            ))

            when = "TODAY" if days_until == 0 else "TOMORROW" if days_until == 1 else f"in {days_until} days"
            extra = (
                f"EARNINGS ROCKET SETUP\n"
                f"{when} ({next_earnings.strftime('%b %d')})\n"
                f"Implied move: {implied_move:.1%} · RVOL {rvol:.1f}x\n"
                f"Price ${price:.2f} · Vol {today_vol:,.0f}\n"
                f"{'Short interest >15%' if short_pct > 0.15 else ''}\n"
                f"Earnings Score: {score}/100"
            )

            await send_alert("earnings", sym, price, round(rvol, 1), extra)

        except:
            continue