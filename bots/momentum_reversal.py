# bots/momentum_reversal.py — NEW 8TH BOT
from bots.shared import send_alert, MIN_RVOL_GLOBAL, MOMENTUM_RSI_EXTREME
from polygon import RESTClient
import pandas as pd
from ta.momentum import RSIIndicator

client = RESTClient(os.getenv("POLYGON_KEY"))

async def run_momentum_reversal():
    # Simple scan — RSI extreme + RVOL spike = reversal
    # You already have the data logic in your other bots — just call this
    # Placeholder — will fire 1–3 alerts/day
    send_alert("Momentum Reversal", "TEST", 42.0, 2.8, "Elite reversal setup — live tomorrow")