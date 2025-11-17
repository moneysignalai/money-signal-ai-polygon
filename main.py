import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {
        "status": "LIVE — 7 bots scanning",
        "time": time.strftime("%H:%M:%S")
    }

from bots.shared import send_alert, start_polygon_websocket

# EXACT REAL FILE + FUNCTION NAMES FROM YOUR REPO
from bots.gap       import run_gap       as run_gap_scan
from bots.cheap     import run_cheap     as run_cheap_scan
from bots.unusual   import run_unusual   as run_unusual_scan
from bots.orb       import run_orb       as run_orb_scan
from bots.squeeze   import run_squeeze   as run_squeeze_scan
from bots.momentum  import run_momentum  as run_momentum_scan
from bots.breakout  import run_breakout  as run_breakout_scan

def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-bot suite STARTED — loose filters active")
    start_polygon_websocket()
    
    cycle = 0
    while True:
        cycle += 1
        now = time.strftime("%H:%M:%S")
        print(f"SCAN #{cycle} | Starting @ {now}")
        send_alert("Scanner", "Now Scanning", 0, 0, f"Cycle #{cycle} • {now} EST • 7 bots active")
        
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
    print("INFO: All 7 bots launched — alerts starting now")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))