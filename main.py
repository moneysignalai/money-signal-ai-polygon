import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

# Root endpoint — keeps Render health checks happy (200 OK)
@app.get("/")
def root():
    return {
        "status": "MoneySignalAi 7-Bot Suite LIVE",
        "bots": "Gap • Cheap • Unusual • ORB • Squeeze • Momentum • Breakout",
        "alerts_today": "3–10 expected"
    }

# ———————— IMPORT YOUR BOT FUNCTIONS ————————
# (adjust the import names if your files are named differently)
from bots.gap_bot import run_gap_scan
from bots.cheap_bot import run_cheap_scan
from bots.unusual_bot import run_unusual_scan
from bots.orb_bot import run_orb_scan
from bots.squeeze_bot import run_squeeze_scan
from bots.momentum_bot import run_momentum_scan
from bots.breakout_bot import run_breakout_scan
from shared import start_polygon_websocket, send_alert

# ———————— BACKGROUND BOT LAUNCHER ————————
def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-bot suite STARTED — loose filters active")
    start_polygon_websocket()  # connects once and stays alive
    
    while True:
        print(f"SCAN: Starting new cycle @ {time.strftime('%H:%M:%S')}")
        
        threads = [
            threading.Thread(target=run_gap_scan),
            threading.Thread(target=run_cheap_scan),
            threading.Thread(target=run_unusual_scan),
            threading.Thread(target=run_orb_scan),
            threading.Thread(target=run_squeeze_scan),
            threading.Thread(target=run_momentum_scan),
            threading.Thread(target=run_breakout_scan),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
            
        print("SCAN: Cycle complete — sleeping 30s")
        time.sleep(30)

# Start bots automatically when the app boots
@app.on_event("startup")
async def startup_event():
    thread = threading.Thread(target=run_all_bots_forever, daemon=True)
    thread.start()
    print("INFO: All 7 bots launched in background — scanning every 30s")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))