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
POLYGON_KEY            = os.getenv("POLYGON_KEY")
TELEGRAM_CHAT_ALL      = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_DEAL    = os.getenv("TELEGRAM_TOKEN_DEAL")
TELEGRAM_TOKEN_EARN    = os.getenv("TELEGRAM_TOKEN_EARN")
TELEGRAM_TOKEN_FLOW    = os.getenv("TELEGRAM_TOKEN_FLOW")
TELEGRAM_TOKEN_GAP     = os.getenv("TELEGRAM_TOKEN_GAP")
TELEGRAM_TOKEN_ORB     = os.getenv("TELEGRAM_TOKEN_ORB")
TELEGRAM_TOKEN_SQUEEZE = os.getenv("TELEGRAM_TOKEN_SQUEEZE")
TELEGRAM_TOKEN_UNUSUAL = os.getenv("TELEGRAM_TOKEN_UNUSUAL")

# Import bots
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

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
    return mapping.get(bot_name.lower(), TELEGRAM_TOKEN_FLOW)  # fallback to FLOW token if needed

# Override send_alert to use your exact env vars
def send_alert(bot_name: str, ticker: str, price: float, rvol: float, extra: str = ""):
    token = get_token_for_bot(bot_name)
    chat_id = TELEGRAM_CHAT_ALL
    if not token or not chat_id:
        print(f"ALERT (no creds): {bot_name} → {ticker} {extra}")
        return
    
    message = f"**{bot_name.upper()}** → **{ticker}** @ ${price:.2f} | RVOL {rvol:.1f}x {extra}".strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        import requests
        requests.post(url, data=payload, timeout=10)
        print(f"TELEGRAM → {message}")
    except:
        print(f"TELEGRAM FAILED → {message}")

from bots.shared import start_polygon_websocket

async def run_all_once():
    await asyncio.gather(
        run_cheap(), run_earnings(), run_gap(),
        run_orb(), run_squeeze(), run_unusual(), run_volume(),
        return_exceptions=True
    )

def run_forever():
    print("INFO: MoneySignalAi 7-BOT SUITE FULLY LIVE")
    start_polygon_websocket()
    
    cycle = 0
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while True:
        cycle += 1
        now = time.strftime("%I:%M:%S %p")
        print(f"SCAN #{cycle} | STARTING @ {now}")
        send_alert("Scanner", "Now Scanning", 0, 0, f"Cycle #{cycle} • {now} EST • 7 BOTS ACTIVE")
        
        loop.run_until_complete(run_all_once())
        print("SCAN: Cycle complete — waiting 30s")
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()
    print("INFO: ALL 7 BOTS LAUNCHED — TELEGRAM ALERTS ACTIVE")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))