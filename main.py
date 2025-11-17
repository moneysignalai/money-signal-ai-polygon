import os
import threading
import time
import asyncio
import requests
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — ALL 7 BOTS ACTIVE", "time": time.strftime("%I:%M:%S %p")}

# Your exact environment variables
POLYGON_KEY            = os.getenv("POLYGON_KEY")
TELEGRAM_CHAT_ALL      = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL    = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN    = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW    = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP     = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB     = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL = os.getenv("TELEGRAM_TOKEN_UNUSUAL")

# Import all 7 bots
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

# Simple Telegram sender using your exact env vars
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = TELEGRAM_TOKEN_FLOW  # fallback token (Volume is always active)
    if bot_name == "Cheap":    token