import os
import threading
import time
import asyncio
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — ALL 7 BOTS SCANNING", "time": time.strftime("%I:%M:%S %p")}

# Use YOUR exact env var names
POLYGON_KEY           = os.getenv("POLYGON_KEY")
TELEGRAM_CHAT_ALL     = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL   = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN   = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW   = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP    = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB    = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE= os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL= os.getenv("TELEGRAM_TOKEN_UNUSUAL")

# Import bots
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume   # this is your "FLOW" bot

# Quick helper so send_alert uses the right token
def get_token_for_bot(bot_name: str) -> str:
    mapping = {
        "cheap":    TELEGRAM_TOKEN_DEAL,
        "earnings": TELEGRAM_TOKEN_EARN,
        "volume":   TELEGRAM_TOKEN_FLOW,    # your "FLOW" token
        "gap":      TELEGRAM_TOKEN_GAP,
        "orb":      TELEGRAM_TOKEN_ORB,
        "squeeze":  TELEGRAM_TOKEN_SQUEEZE,
        "unusual":  TELEGRAM_TOKEN_UNUSUAL,
    }
    return mapping.get(bot_name.lower(), TELEGRAM_CHAT_ALL)  # fallback to private chat

# Override send_alert to use your exact env vars
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = get_token_for_bot(bot_name)
    chat_id = TELEGRAM_CHAT_ALL
    if not token or not chat_id:
        print(f"ALERT (no creds): {bot_name} → {ticker} {extra}")
        return
    
    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".