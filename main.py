import os
import threading
import time
import asyncio
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — ALL 7 BOTS RUNNING", "time": time.strftime("%I:%M:%S %p")}

from bots.shared import send_alert, start_polygon_websocket

# All 7 real bots from your repo
from bots.cheap    import run_cheap
from bots.earnings import run_earnings
from bots.gap      import run_gap
from bots.orb      import run_orb
from bots.squeeze  import run_squeeze
from bots.unusual  import run_unusual
from bots.volume   import run_volume

async def run_all_bots_once():
    tasks = [
        run_cheap(),
        run_earnings(),
        run_gap(),
        run_orb(),
        run_squeeze(),
        run_unusual(),
        run_volume(),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)  # Won't crash if one fails

def run_all_bots_forever():
    print("INFO: MoneySignalAi 7-BOT SUITE FULLY LIVE — LOOSENED FILTERS")
    start_polygon_websocket()
    
    cycle = 0
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while True:
        cycle += 1
        now = time.strftime("%I:%M:%S %p")
        print(f"SCAN #{cycle} | STARTING @ {now}")
        send_alert("Scanner", "Now Scanning", 0, 0, f"Cycle #{cycle} • {now} EST • 7 BOTS ACTIVE")
        
        loop.run_until_complete(run_all_bots_once())
        
        print("SCAN: Cycle complete — waiting 30s")
        time.sleep(30)

@app.on_event("startup")
async def startup_event():
    threading.Thread(target=run_all_bots_forever, daemon=True).start()
    print("INFO: ALL 7 ASYNC BOTS LAUNCHED — ALERTS STARTING NOW")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))