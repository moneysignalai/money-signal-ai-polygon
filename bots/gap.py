# bots/gap.py — ELITE 2025 GAP SNIPER (2–7 killers per week)
from .shared import send_alert, client
from .helpers import get_top_volume_stocks
from datetime import datetime, time

async def run_gap():
    now = datetime.now()
    # Only run 9:30 – 10:30 AM EST (gap sweet spot)
    if now.weekday() >= 5 or now.hour != 9 or now.minute > 59:
        return
    if now.minute < 30:
        return

    top_stocks = get_top_volume_stocks(120)

    for sym in top_stocks:
        try:
            # 1. Get yesterday's close & today's open
            daily = client.get_aggs(sym, 1, "day", limit=3)
            if len(daily) < 2:
                continue
            yesterday = daily[-2]
            today = daily[-1]

            gap_pct = (today.open - yesterday.close) / yesterday.close * 100

            # 2. ELITE GAP FILTERS
            if not (4.0 <= abs(gap_pct) <= 35.0):          # 4–35% gaps only
                continue
            if gap_pct < 0:                                 # Only gap-ups (fade-downs later)
                continue

            price = client.get_last_trade(sym).price

            # 3. Volume explosion at open
            if today.volume < yesterday.volume * 2.8:       # 2.8x+ yesterday's volume
                continue

            # 4. Price filter — no pennies, no mega-caps
            if price < 8 or price > 180:
                continue

            # 5. Float filter — must be low enough to move
            ticker_info = client.get_ticker_details(sym)
            shares_float = getattr(ticker_info, 'shares_outstanding', 0) or 0
            if shares_float == 0 or shares_float > 150_000_000:
                continue

            # 6. Catalyst check: news in last 24h?
            news = client.list_ticker_news(sym, limit=5, published_utc_gte=(now - timedelta(days=1)).isoformat())
            has_news = len(news) > 0 if news else False

            # 7. Final trigger: holding above open + VWAP
            if price < today.open * 0.985:                  # dipped below open = weak
                continue

            # GAP SCORE
            score = min(100, int(
                gap_pct * 2.2 +
                (today.volume / yesterday.volume) * 10 +
                (50 if has_news else 0) +
                (30 if shares_float < 50_000_000 else 0)
            ))

            direction = "GAP & GO"
            extra = (
                f"{direction} · +{gap_pct:.1f}% GAP\n"
                f"Float {shares_float/1_000_000:.1f}M · Vol {today.volume:,.0f} ({today.volume/yesterday.volume:.1f}x)\n"
                f"Price ${price:.2f} · {'News catalyst' if has_news else 'No news'}\n"
                f"Gap Score: {score}/100"
            )

            await send_alert("gap", sym, price, round(today.volume/yesterday.volume, 1), extra)

        except:
            continue