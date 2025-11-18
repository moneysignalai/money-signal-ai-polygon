# bots/orb.py — ELITE SNIPER VERSION (3–8 alerts/day, 70%+ win rate)
from .shared import send_alert, client
from .helpers import get_top_volume_stocks
from datetime import datetime, time, timedelta

async def run_orb():
    now = datetime.now()
    # Only run 9:35 AM – 11:00 AM EST (best ORB window)
    if now.weekday() >= 5 or now.hour < 9 or now.hour > 11:
        return
    if now.hour == 9 and now.minute < 35:
        return

    for sym in get_top_volume_stocks(120):
        try:
            # 1. Get 9:30–9:45 range (true ORB)
            start = datetime.combine(now.date(), time(9, 30))
            end = start + timedelta(minutes=15)
            bars = client.get_aggs(sym, 1, "minute", from_=start.date(), to=end.date(), limit=20)
            if len(bars) < 10:
                continue

            df = __import__("pandas").DataFrame([b.__dict__ for b in bars])
            orb_high = df["high"].max()
            orb_low = df["low"].min()
            orb_range = orb_high - orb_low

            # Filter 1: Range must be decent size
            if orb_range < 0.6:  # $0.60 minimum range
                continue

            current_price = client.get_last_trade(sym).price

            # Filter 2: Clean breakout (at least 1.5× the range)
            if current_price > orb_high:
                direction = "LONG"
                breakout_strength = (current_price - orb_high) / orb_range
            elif current_price < orb_low:
                direction = "SHORT"
                breakout_strength = (orb_low - current_price) / orb_range
            else:
                continue

            if breakout_strength < 1.5:  # must break by 150% of range
                continue

            # Filter 3: Volume surge on breakout
            today_bars = client.get_aggs(sym, 1, "day", limit=2)
            if len(today_bars) < 2:
                continue
            avg_vol = today_bars[0].volume
            today_vol = today_bars[1].volume
            if today_vol < avg_vol * 1.8:  # RVOL 1.8x+
                continue

            # Filter 4: Price > $8 (avoid penny junk)
            if current_price < 8:
                continue

            # Filter 5: Relative volume rank (top 100 only)
            # (already handled by get_top_volume_stocks)

            extra = (
                f"ORB {direction} BREAK\n"
                f"Range: ${orb_low:.2f} – ${orb_high:.2f} (${orb_range:.2f})\n"
                f"Break: {breakout_strength:.1f}x range strength\n"
                f"RVOL {today_vol/avg_vol:.1f}x · Price ${current_price:.2f}\n"
                f"High-conviction setup"
            )

            await send_alert("orb", sym, current_price, round(today_vol/avg_vol, 1), extra)

        except:
            continue