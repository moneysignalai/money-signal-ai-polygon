import os
import threading
import time
import sys
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {
        "status": "MoneySignalAi LIVE — bots scanning",
        "uptime": time.strftime("%H:%M:%S")
    }

from shared import start_polygon_websocket, send_alert

# ———————— SAFE IMPORTS (no crash if file missing) ————————
bots_available = {}
try:
    from bots.gap_bot import run_gap_scan
    bots_available['gap'] = run_gap_scan
    print("SUCCESS: Imported GapBot")
except ImportError as e:
    print(f"WARNING: GapBot import failed: {e}")

try:
    from bots.cheap_bot import run_cheap_scan
    bots_available['cheap'] = run_cheap_scan
    print("SUCCESS: Imported CheapBot")
except ImportError as e:
    print(f"WARNING: CheapBot import failed: {e}")

try:
    from bots.unusual import run_unusual_scan  # From your earlier commit, it's unusual.py
    bots_available['unusual'] = run_unusual_scan
    print("SUCCESS: Imported UnusualBot")
except ImportError as e:
    print(f"WARNING: UnusualBot import failed: {e}")

try:
    from bots.orb_bot import run_orb_scan
    bots_available['orb'] = run_orb_scan
    print("SUCCESS: Imported ORBBot")
except ImportError as e:
    print(f"WARNING: ORBBot import failed: {e}")

try:
    from bots.squeeze_bot import run_squeeze_scan
    bots_available['squeeze'] = run_squeeze_scan
    print("SUCCESS: Imported SqueezeBot")
except ImportError as e:
    print(f"WARNING: SqueezeBot import failed: {e}")

# Add the other two later — for now, start with these 5
print(f"INFO: Loaded {len(bots_available)} bots: {list(bots_available.keys())}")

# ———————— BACKGROUND RUNNER (only runs what we have) ————————
def run_all_bots_forever():
    print("INFO: MoneySignalAi suite STARTED — loose filters active")
    start_polygon_websocket()
    
    while True:
        print(f"SCAN: Starting cycle @ {time.strftime('%H:%M:%S')} with {len(bots_available)} bots")
        
        threads = [threading.Thread(target=func) for func in bots_available.values()]
        
        for t in threads: t.start()
        for t in threads: t.join()
            
        print("SCAN: Cycle done — 30s sleep")
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    if bots_available:
        thread = threading.Thread(target=run_all_bots_forever, daemon=True)
        thread.start()
        print("INFO: Background scanning launched — alerts incoming")
    else:
        print("ERROR: No bots loaded — check file names in bots/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))