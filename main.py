import os
import threading
import time
import asyncio
from fastapi import FastAPI
import uvicorn

# ←←← NEW LINE #1 — ADD THIS IMPORT
from status import send_status

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — ALL 7 BOTS SCANNING", "time": time.strftime("%I:%M:%S %p")}

from bots.shared import send_alert, start_polygon_websocket
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

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
        
        # ←←← NEW LINE #2 — SENDS STATUS REPORT EVERY ~60 MINUTES
        if cycle % 120 == 0:
            send_status()
        
        loop.run_until_complete(run_all_once())
        print("SCAN: Cycle complete — waiting 30s")
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_forever, daemon=True).start()
    print("INFO: ALL 7 BOTS + STATUS REPORT LAUNCHED")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))