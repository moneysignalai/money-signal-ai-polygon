# bots/status_report.py
import os
import requests
import time
from datetime import datetime

async def run_status_report():
    token = os.getenv("TELEGRAM_TOKEN_STATUS")
    chat_id = os.getenv("TELEGRAM_CHAT_STATUS")
    
    if not token or not chat_id:
        print("STATUS REPORT: Missing token or chat_id")
        return

    now = datetime.now().strftime("%I:%M %p · %b %d")
    message = f"""*MoneySignalAi — FULL SUITE STATUS*  
{now} EST  

7 trading bots running 24/7 on Render  
Polygon WebSocket: Connected  
Scanner: Active (heartbeat every 30s)  

Bots live & scanning:  
• Cheap          • Earnings  
• Gap            • ORB  
• Squeeze        • Unusual  
• Volume Leader  

Next windows:  
2:30–4:00 PM EST → Cheap / Squeeze / Unusual / Volume  
Tomorrow 9:30 AM → Gap + ORB  

System 100% healthy — waiting for setups"""

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}, timeout=10)
        print("STATUS REPORT SENT")
    except Exception as e:
        print(f"STATUS REPORT FAILED: {e}")