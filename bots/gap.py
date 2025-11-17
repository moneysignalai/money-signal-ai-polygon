# bots/gap.py
from .shared import *
from datetime import datetime, timedelta

async def run_gap():
    now = datetime.now()
    if now.hour != 9 or now.minute != 30 or now.weekday() >= 5: return

    universe = await get_top_500_universe()
    gaps = []
    for sym in universe:
        try:
            # Yesterday's close
            prev_day = polygon_client.get_aggs(sym, 1, "day", limit=2)
            if len(prev_day.results) < 2: continue
            prev_close = prev_day.results[0].close

            # Today's open
            today_open = polygon_client.get_last_trade(sym).price
            gap_pct = (today_open - prev_close) / prev_close * 100

            if abs(gap_pct) < 3: continue

            # Volume filter
            vol = polygon_client.get_aggs(sym, 1, "minute", limit=1).results[0].volume
            avg_vol = polygon_client.get_aggs(sym, 1, "day", limit=30).results_df['v'].mean()
            if vol < 2 * avg_vol: continue

            # RSI
            rsi = RSIIndicator(polygon_client.get_aggs(sym, 1, "minute", limit=20).results_df['c']).rsi().iloc[-1]

            # Direction
            direction = "FADE" if gap_pct > 0 else "FILL"
            opt_type = "put" if direction == "FADE" else "call"

            # MTF
            if not mtf_confirm(sym, "SHORT" if direction == "FADE" else "LONG"): continue

            # Option
            exp = now.strftime("%Y-%m-%d")  # 0DTE
            contracts_resp = polygon_client.list_options_contracts(underlying_ticker=sym, expiration_date=exp, contract_type=opt_type)
            contracts = contracts_resp.results
            atm = [c for c in contracts if abs(c.strike_price - today_open) < 3]
            if not atm: continue
            best = max(atm, key=lambda x: x.volume)
            oquote = get_greeks(sym, exp, best.strike_price, opt_type)
            if not oquote or not is_edge_option(oquote): continue

            link = build_rh_link(sym, exp, best.strike_price, opt_type)
            confidence = get_confidence_score(oquote.volume/oquote.open_interest, oquote.gamma, oquote.implied_volatility, True, True)
            body = f"*{direction} GAP {sym}* | Gap: {gap_pct:+.1f}%\nBuy 2x {int(best.strike_price)}{opt_type[0]} @ ${oquote.ask:.2f} | Exp: {exp[5:10].replace('-','/')} (0DTE)\nIV: {oquote.implied_volatility:.0%} | Delta: {oquote.delta:.2f} | Gamma: {oquote.gamma:.2f}\nVol: {oquote.volume:,} | OI: {oquote.open_interest:,} | V/OI: {oquote.volume/oquote.open_interest:.1f}x\nRSI: {rsi:.0f} | MTF: {'DOWN' if direction=='FADE' else 'UP'}\n**Confidence: {confidence}/100 {'⭐'* (confidence//20)}**\nEntry: Open | Exit: 50% @ +80% | 50% @ +150% | Trail"
            await send_alert(os.getenv("TELEGRAM_TOKEN_GAP", os.getenv("TELEGRAM_TOKEN_ORB")), f"GAP {direction} {sym}", body, link, confidence)
        except: pass
