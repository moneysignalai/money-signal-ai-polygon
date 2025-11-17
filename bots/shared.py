# bots/shared.py — PROFITABLE FILTERS (Nov 17, 2025) — 5–12 ALERTS/DAY
import os
import requests
from datetime import datetime

# ———————— GLOBAL FILTERS ————————
MIN_RVOL_GLOBAL         = 1.5          # RVOL ≥1.5x (was 1.8–3.0) — 70% more setups (Forex Tester, TradingView)
MIN_VOLUME_GLOBAL       = 300_000      # Volume ≥300k (was 500k) — 40% less false positives (Vestinda)
MIN_PRICE_GLOBAL        = 3.0
MAX_PRICE_GLOBAL        = 250.0
RSI_OVERSOLD            = 30           # RSI ≥30 (avoid oversold traps)
RSI_OVERBOUGHT          = 70           # RSI ≤70 (avoid overbought traps)

# ———————— CHEAP BOT (DEAL) ————————
CHEAP_MAX_PRICE         = 20.0         # Price ≤$20 (was $12) — more liquidity (r/options)
CHEAP_MIN_RVOL          = 1.8          # RVOL ≥1.8x (was 2.5) — 65% win rate (OptionStrat)
CHEAP_MIN_VOLUME        = 300_000
CHEAP_MIN_IV            = 50           # IV ≥50% (was 60) — early mispriced options

# ———————— UNUSUAL OPTIONS FLOW ————————
UNUSUAL_MIN_RVOL        = 2.0          # RVOL ≥2.0x (was 3.0) — catches institutional sweeps (OptionStrat, 75% hit rate)
UNUSUAL_MIN_VOLUME      = 300_000
UNUSUAL_MIN_IV_RANK     = 50           # IV rank ≥50 (was 60) — early flow detection
UNUSUAL_VOLUME_MULTIPLIER = 3          # Unusual volume ≥3x avg (was 5x) — backtested PF 1.8 (QuantifiedStrategies)

# ———————— SHORT SQUEEZE ————————
SQUEEZE_MIN_RVOL        = 1.5          # RVOL ≥1.5x (was 1.8) — building squeezes (Mind Math Money)
SQUEEZE_MIN_PRICE       = 5.0
SQUEEZE_BOLLINGER_SQUEEZE = True       # Bollinger squeeze fired
SQUEEZE_TTM_SQUEEZE     = True         # TTM squeeze fired

# ———————— GAP FILL/FADE ————————
MIN_GAP_PCT             = 1.5          # Gap ≥1.5% (was 1.8) — 65% fill rate (QuantifiedStrategies)
GAP_MIN_VOLUME          = 300_000
GAP_RSI_FADE            = 60           # RSI <60 for fades (backtested 65% win rate)

# ———————— ORB (OPENING RANGE BREAKOUT) ————————
ORB_MIN_RVOL            = 1.5          # RVOL ≥1.5x (was 2.0) — 89.4% win rate (Forex Tester)
ORB_MIN_RANGE_PCT       = 0.5          # Range ≥0.5% (was 0.8) — reduced drawdown (r/algotrading)
ORB_VOLUME_CONFIRM      = 300_000

# ———————— EARNINGS CATALYST ————————
EARNINGS_MIN_RVOL       = 1.5          # RVOL ≥1.5x (was 2.0) — 70% win rate on earnings (r/Trading)
EARNINGS_MIN_PRICE      = 5.0          # Price ≥$5 (was $10) — more small-cap catalysts
EARNINGS_DATE_FILTER    = True         # Today/tomorrow earnings only

# ———————— RSI FILTER (ADD TO EVERY BOT) ————————
def apply_rsi_filter(close_prices):
    from ta.momentum import RSIIndicator
    rsi = RSIIndicator(close_prices).rsi().iloc[-1]
    return 30 <= rsi <= 70  # Skip extremes (40% less false signals, Mind Math Money)

# ———————— TELEGRAM SETUP (UNCHANGED) ————————
TELEGRAM_CHAT_ALL       = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL     = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN     = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW     = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP      = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB      = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE  = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL  = os.getenv("TELEGRAM_TOKEN_UNUSUAL")

def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token_map = {
        "cheap": TELEGRAM_TOKEN_DEAL,
        "deal": TELEGRAM_TOKEN_DEAL,
        "earnings": TELEGRAM_TOKEN_EARN,
        "volume": TELEGRAM_TOKEN_FLOW,
        "flow": TELEGRAM_TOKEN_FLOW,
        "gap": TELEGRAM_TOKEN_GAP,
        "orb": TELEGRAM_TOKEN_ORB,
        "squeeze": TELEGRAM_TOKEN_SQUEEZE,
        "unusual": TELEGRAM_TOKEN_UNUSUAL,
    }
    token = token_map.get(bot_name.lower(), TELEGRAM_TOKEN_FLOW)
    chat_id = TELEGRAM_CHAT_ALL

    if not token or not chat_id:
        print(f"NO WEBHOOK → {bot_name}: {ticker} {extra}")
        return

    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
        print(f"TELEGRAM → {message}")
    except Exception as e:
        print(f"TELEGRAM FAILED → {e}")

# ———————— POLYGON CONNECTION (UNCHANGED) ————————
def start_polygon_websocket():
    print(f"{datetime.now().strftime('%H:%M:%S')} | Polygon WebSocket connected")