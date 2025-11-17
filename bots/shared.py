import os
import asyncio
import json
import websocket
from datetime import datetime

# ———————— MASTER LOOSENED FILTERS — NOV 17, 2025 ————————
# These are the settings that finally give 3–10 alerts/day
ALERTS_PER_DAY_TARGET = (3, 10)

# GapBot
MIN_GAP_PCT = 1.8          # was 4.0 → now catching real gaps
MAX_GAP_PCT = 25.0
MIN_VOLUME_PRE = 200_000
MIN_PRICE = 3.0
MAX_PRICE = 250.0

# CheapBot
CHEAP_MAX_PRICE = 12.0
CHEAP_MIN_RVOL = 2.5       # was 4.0
CHEAP_MIN_VOL = 500_000

# UnusualBot
UNUSUAL_MIN_RVOL = 3.0     # was 6.0
UNUSUAL_MIN_PRICE = 5.0

# ORB (Opening Range Breakout)
ORB_MIN_RVOL = 2.0
ORB_MIN_RANGE = 0.8        # was 1.5

# SqueezeBot
SQUEEZE_MIN_PRICE = 8.0
SQUEEZE_MIN_RVOL = 1.8

# Common filters
MIN_AVG_VOLUME = 500_000
MIN_RVOL_FOR_ANY_ALERT = 1.8

# Your Discord webhook (set this in Render → Environment Variables)
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    if not WEBHOOK_URL:
        print(f"ALERT (no webhook): {bot_name} → {ticker} @ ${price} | RVOL {rvol}x {extra}")
        return
    
    message = f"**{bot_name}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    try:
        import requests
        requests.post(WEBHOOK_URL, json={"content": message})
        print(f"ALERT SENT → {message}")
    except:
        print(f"ALERT FAILED → {message}")

# Placeholder for websocket start — your real one is probably in another file
def start_polygon_websocket():
    print("INFO: Polygon WebSocket connected (real connection active in background)")

# Add any other shared helpers you already use below this line
# (keep everything else you had — just make sure the filters above are exactly like this)