import os
import threading
import time
from fastapi import FastAPI
import uvicorn

app = FastAPI(title="MoneySignalAi 7-Bot Suite")

@app.get("/")
def root():
    return {"status": "LIVE — scanning active", "time": time.strftime("%H:%M:%S")}

# Import shared (we know this works now)
from bots.shared import send_alert, start_polygon_websocket

# ————— SAFE IMPORTS — will not crash if a bot is missing/wrong name —————
available_bots = []

def safe_import(module_name, func_name, bot_name):
    try:
        module = __import__(f"bots.{module_name}", fromlist=[func_name])
        func = getattr(module, func_name)
        available_bots.append((func, bot_name))
        print(f"SUCCESS → {bot_name} loaded")
    except Exception as e:
        print(f"FAILED → {bot_name} ({module_name}.py) → {e}")

# Try to load all 7 bots — change the filenames/function names below to match YOUR real files
safe_import("gap",         "run_gap_scan",        "GapBot")
safe_import("cheap_bot",   "run_cheap_scan",      "CheapBot")      # or "cheap" if yours is cheap.py
safe_import("unusual",     "run_unusual_scan",    "UnusualBot")
safe_import("orb_bot",     "run_orb_scan",        "ORBBot")        # or "orb"
safe_import("squeeze_bot", "run_squeeze_scan",    "SqueezeBot")    # or "squeeze"
safe_import("momentum_bot","run_momentum_scan",   "MomentumBot")
safe_import("breakout_bot","run_breakout_scan",   "BreakoutBot")

print(f"INFO: Loaded {len(available_bots)} bots successfully")

def run_all_bots_forever():
    print("INFO: MoneySignalAi suite STARTED — scanning every 30s")
    start_polygon_websocket()