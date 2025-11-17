import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {
        "status": "MoneySignalAi 7-Bot Suite — LIVE & SCANNING",
        "bots": 7,
        "alerts_per_day": "3–10 (loose filters active)",
        "time": time.strftime("%H:%M:%S")
    }

# Correct imports — shared.py is inside bots/
from bots.shared import send_alert, start_polygon_websocket
from bots.gap_bot import run_gap_scan
from bots.cheap_bot import run_cheap_scan
from bots.unusual import run_unusual_scan
from bots.orb_bot import run_orb_scan
from bots.squeeze_bot import run_squeeze_scan
from bots.momentum_bot import run_momentum_scan
from bots.breakout_bot import run_breakout_scan

def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-bot suite STARTED — loose filters active")
    start_polygon_websocket()
    
    cycle = 0
    while True:
        cycle += 1
        now = time.strftime("%H:%M:%S")
        print(f"SCAN #{cycle} | Starting scan cycle @ {now}")
        
        # ←←← This sends the "Now Scanning…" message once per cycle ←←←
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
    thread = threading.Thread(target=run_all_bots_forever, daemon=True)
    thread.start()
    print("INFO: All 7 bots + scan ping launched — you’ll see activity every 30s")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))