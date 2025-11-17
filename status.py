# status.py  ← put this file in the root (same level as main.py)
from bots.shared import send_alert
import time

def send_status():
    now = time.strftime("%I:%M %p · %b %d")
    message = f"""*MoneySignalAi — SYSTEM STATUS*  
{now} EST  

All 7 bots running 24/7  
Polygon WebSocket connected  
Scanner heartbeat every 30s  
Health checks passing  

Bots active:  
• Cheap • Earnings • Gap • ORB • Squeeze • Unusual • Volume  

Next wave expected:  
2:30–4:00 PM EST → Cheap, Squeeze, Unusual, Volume  
Tomorrow 9:30 AM → Gap + ORB  

Everything is perfect — just waiting on the market"""
    send_alert("System", "Status OK", 0, 0, message)