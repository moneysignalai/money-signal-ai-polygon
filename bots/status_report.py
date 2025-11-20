# status_report.py — manual system status blast for MoneySignalAI

from bots.shared import send_alert
import time

def send_status():
    now = time.strftime("%I:%M %p · %b %d")
    message = f"""*MoneySignalAI — SYSTEM STATUS*  
{now} EST  

All core scanners are running and connected to Polygon.  
Telegram alert pipeline is live and responding.  

*Active strategies (15-in-1 suite):*  
• Premarket Runner  
• Gap & Go (up & down)  
• ORB (Opening Range Breakout)  
• Volume Monster  
• Cheap 0–5 DTE Options  
• Unusual Options Sweeps  
• Whale Flow ($2M+ orders)  
• Short Squeeze Pro  
• Earnings Move + Fundamentals  
• Momentum Reversal  
• Swing Pullback  
• Panic Flush  
• Trend Rider (Daily Breakouts)  
• IV Crush (Post-Earnings)  
• Dark Pool Radar  

*Typical hunt windows (EST):*  
• 04:00–09:30 — Premarket, Dark Pool Radar  
• 09:30–10:30 — Gap & Go, ORB, Volume spikes  
• 09:30–16:00 — Cheap, Unusual, Whales, Squeeze, Momentum, Panic, Swing  
• 15:30–20:15 — Trend Rider, Dark Pool Radar, late Earnings/IV moves  

Everything is armed and watching the tape for:  
• Explosive volume  
• Big options flow  
• Key earnings movers  
• Dark pool clusters  
• High-probability reversals & breakouts  

You focus on execution.  
*Let the bots watch the market. ⚡*"""
    send_alert("System", "Status OK", 0, 0, message)
