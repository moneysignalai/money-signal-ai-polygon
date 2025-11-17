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

# ←←← CORRECT IMPORTS — shared.py is inside bots/ folder ←←←
from bots.shared import send_alert, start_polygon_websocket

# Import all 7 bots (adjust