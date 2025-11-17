import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — ALL 7 BOTS SCANNING", "time": time.strftime("%H:%M:%S")}

from bots.shared import send_alert, start_polygon_websocket

# EXACT FILENAMES THAT EXIST IN YOUR REPO RIGHT NOW (Nov 17, 2025)
from bots.gap       import run_gap       as run_gap_scan
from bots.cheap     import run_cheap     as run_cheap_scan
from bots.unusual   import run_unusual   as run_unusual_scan
from bots.orb       import run_orb       as run_orb_scan
from bots.squeeze   import run_squeeze   as run_squeeze_scan
from bots.momentum  import run_momentum  as run_momentum_scan
from bots.breakout  import run_breakout  as run_breakout_scan

def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-BOT SUITE FULLY LIVE — loose filters active")
    start_polygon_websocket()
    
    cycle = 0
    while True:
        cycle += 1
        now = time.strftime("%H:%M:%S")
        print(f"SCAN #{cycle} | Starting @ {now}")
        send_alert("Scanner", "Now Scanning", 0, 0, f"Cycle #{cycle} • {now} EST • ALL 7 BOTS ACTIVE")
        
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
    threading.Thread(target=run_all_bots_forever, daemon=True).start()
    print("INFO: ALL 7 BOTS LAUNCHED — ALERTS STARTING NOW")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))