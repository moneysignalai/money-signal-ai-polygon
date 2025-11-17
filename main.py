import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {
        "status": "MoneySignalAi 7-Bot Suite LIVE",
        "message": "All 7 bots running in background — scanning every 30s",
        "time": time.strftime("%H:%M:%S")
    }

# ———————— CORRECT IMPORTS FOR bots/ FOLDER ————————
from bots.gap_bot import run_gap_scan
from bots.cheap_bot import run_cheap_scan
from bots.unusual import run_unusual_scan          # ← your file is named unusual.py
from bots.orb_bot import run_orb_scan
from bots.squeeze_bot import run_squeeze_scan
from bots.momentum_bot import run_momentum_scan    # or whatever the real name is
from bots.breakout_bot import run_breakout_scan    # or whatever the real name is

from shared import start_polygon_websocket, send_alert

# ———————— BACKGROUND BOT RUNNER ————————
def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-bot suite STARTED — loose filters active")
    start_polygon_websocket()
    
    while True:
        print(f"SCAN: Starting new scan cycle @ {time.strftime('%H:%M:%S')}")
        
        threads = [
            threading.Thread(target=run_gap_scan),
            threading.Thread(target=run_cheap_scan),
            threading.Thread(target=run_unusual_scan),
            threading.Thread(target=run_orb_scan),
            threading.Thread(target=run_squeeze_scan),
            threading.Thread(target=run_momentum_scan),
            threading.Thread(target=run_breakout_scan),
        ]
        
        for t in threads: t.start()
        for t in threads: t.join()
            
        print("SCAN: Cycle complete — waiting 30s")
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    thread = threading.Thread(target=run_all_bots_forever, daemon=True)
    thread.start()
    print("INFO: All 7 bots launched in background — scanning live")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))