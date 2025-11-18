# bots/squeeze.py — ELITE 2025 MONSTER SQUEEZE DETECTOR (2–6 killers per week)
from .shared import send_alert, client
from .helpers import get_top_volume_stocks
from datetime import datetime, timedelta

async def run_squeeze():
    # Only scan during regular hours + power hour
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 9 or now.hour >= 16:
        return

    top_stocks = get_top_volume_stocks(150)

    for sym in top_stocks:
        try:
            # 1. Get short interest (must be high)
            si = client.get_short_interest(sym, limit=3)
            if not si or len(si) == 0:
                continue
            latest_si = si[0]
            short_ratio = latest_si.short_interest / latest_si.float_shares if latest_si.float_shares else 0

            if short_ratio < 0.22:  # 22%+ of float short (was 18%)
                continue

            # 2. Days to cover — the real killer
            avg_vol_20d = latest_si.average_volume_20d or 1
            days_to_cover = latest_si.short_interest / avg_vol_20d
            if days_to_cover < 5.5:  # 5.5+ days to cover = trapped shorts
                continue

            # 3. Price action: must be up strong today
            daily = client.get_aggs(sym, 1, "day", limit=3)
            if len(daily) < 2:
                continue
            today = daily[-1]
            yesterday = daily[-2]
            gain_today = (today.close - yesterday.close) / yesterday.close

            if gain_today < 0.09:  # +9% or more today
                continue

            # 4. Volume explosion
            if today.volume < yesterday.volume * 2.2:  # 2.2x+ yesterday's volume
                continue

            # 5. Price filter — no pennies
            price = client.get_last_trade(sym).price
            if price < 6.0 or price > 200:
                continue

            # 6. Final trigger: accelerating momentum (last 30 min)
            recent = client.get_aggs(sym, 5, "minute", limit=6)  # last 30 min
            if len(recent) >= 4 and recent[-1].close > recent[-4].close * 1.04:
                momentum = "ACCELERATING"
            else:
                momentum = "STRONG"

            # SQUEEZE SCORE (out of 100)
            score = min(100, int(
                short_ratio * 200 +
                days_to_cover * 6 +
                gain_today * 100 +
                10
            ))

            extra = (
                f"MONSTER SQUEEZE IN PLAY\n"
                f"Short: {short_ratio:.1%} of float · DTC: {days_to_cover:.1f}\n"
                f"Today: +{gain_today:.1%} · Vol {today.volume:,.0f} ({today.volume/yesterday.volume:.1f}x)\n"
                f"Price ${price:.2f} · {momentum}\n"
                f"Squeeze Score: {score}/100"
            )

            await send_alert("squeeze", sym, price, round(today.volume/yesterday.volume, 1), extra)

        except:
            continue