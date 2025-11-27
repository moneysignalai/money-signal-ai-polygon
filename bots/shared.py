# bots/status_report.py

import os
import json
import time
from dataclasses import dataclass, asdict
from typing import Dict, Any

from bots.shared import now_est  # reuse your EST timestamp helper

import requests

# Where we persist per-bot stats between scans
STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")

# Heartbeat minimum interval (minutes)
HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))

# Telegram routing (reuse same envs you already use)
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")

# If status token not set, fall back to alerts token
_TELEGRAM_STATUS_TOKEN = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS


@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    last_runtime: float = 0.0
    last_run_ts: float = 0.0           # unix timestamp
    last_run_str: str = ""             # pretty EST string


def _send_telegram_status(text: str) -> None:
    token = _TELEGRAM_STATUS_TOKEN
    chat = TELEGRAM_CHAT_ALL
    if not token or not chat:
        print(f"[status_report] (no TELEGRAM_STATUS_TOKEN/TELEGRAM_CHAT_ALL) {text}")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": text}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[status_report] failed to send heartbeat: {e} | text={text!r}")


def _load_stats() -> Dict[str, Any]:
    """
    Stats file structure:
    {
      "last_heartbeat_ts": float,
      "bots": {
        "premarket": {...},
        "options_flow": {...},
        ...
      }
    }
    """
    try:
        if not os.path.exists(STATS_PATH):
            return {"last_heartbeat_ts": 0.0, "bots": {}}
        with open(STATS_PATH, "r") as f:
            data = json.load(f)
        if "bots" not in data:
            data["bots"] = {}
        if "last_heartbeat_ts" not in data:
            data["last_heartbeat_ts"] = 0.0
        return data
    except Exception as e:
        print(f"[status_report] _load_stats error: {e}")
        return {"last_heartbeat_ts": 0.0, "bots": {}}


def _save_stats(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    except Exception:
        pass
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[status_report] _save_stats error: {e}")


def record_bot_stats(
    bot_name: str,
    scanned: int,
    matched: int,
    alerts: int,
    runtime: float,
) -> None:
    """
    Called by each bot once per scan cycle.
    API is already used by your bots; DO NOT CHANGE THE SIGNATURE.
    """
    data = _load_stats()
    bots = data.get("bots", {})

    now_ts = time.time()
    pretty_ts = now_est()

    prev = bots.get(bot_name) or {}
    stats = BotStats(
        bot_name=bot_name,
        scanned=int(scanned),
        matched=int(matched),
        alerts=int(alerts),
        last_runtime=float(runtime),
        last_run_ts=now_ts,
        last_run_str=pretty_ts,
    )

    # merge with previous if you ever want cumulative metrics later
    bots[bot_name] = asdict(stats)
    data["bots"] = bots
    _save_stats(data)


async def run_status_report():
    """
    Periodic heartbeat:
      • Shows per-bot "OK @ time"
      • Shows global totals
      • Shows per-bot scanned / matches / alerts
      • Shows top 3 by scanned (heaviest bots)
    """
    data = _load_stats()
    bots = data.get("bots", {})
    last_hb = float(data.get("last_heartbeat_ts", 0.0))
    now_ts = time.time()

    # Heartbeat interval guard
    min_interval_sec = HEARTBEAT_INTERVAL_MIN * 60.0
    if now_ts - last_hb < min_interval_sec:
        print("[status_report] Heartbeat skipped (interval).")
        return

    if not bots:
        print("[status_report] no bot stats yet; skipping heartbeat.")
        return

    # Compute totals
    total_scanned = 0
    total_matched = 0
    total_alerts = 0

    # We'll also build a list for sorting by scans
    bot_rows = []
    for name, raw in bots.items():
        scanned = int(raw.get("scanned", 0))
        matched = int(raw.get("matched", 0))
        alerts = int(raw.get("alerts", 0))
        last_run_str = raw.get("last_run_str") or "N/A"
        last_runtime = float(raw.get("last_runtime", 0.0))

        total_scanned += scanned
        total_matched += matched
        total_alerts += alerts

        bot_rows.append(
            {
                "name": name,
                "scanned": scanned,
                "matched": matched,
                "alerts": alerts,
                "last_run_str": last_run_str,
                "last_runtime": last_runtime,
            }
        )

    # Sort for display: by name for status list, by scanned desc for "heaviest"
    bot_rows_by_name = sorted(bot_rows, key=lambda r: r["name"])
    bot_rows_by_scans = sorted(bot_rows, key=lambda r: r["scanned"], reverse=True)

    # ---------------- BUILD HEARTBEAT TEXT ----------------

    ts_str = now_est()

    lines: list[str] = []
    lines.append("📡 MoneySignalAI Heartbeat ❤️")
    lines.append(f"⏰ {ts_str}")
    lines.append("────────────────")
    lines.append("✅ ALL SYSTEMS GOOD")
    lines.append("")
    lines.append("🤖 Bot Status:")

    # Per-bot status line, like before
    for r in bot_rows_by_name:
        name = r["name"]
        last_run_str = r["last_run_str"]
        lines.append(f"• ✅ {name}: OK @ {last_run_str}")

    lines.append("────────────────")
    lines.append("📊 Scanner Analytics:")
    lines.append(f"• Total scanned: **{total_scanned:,}**")
    lines.append(f"• Filter matches: **{total_matched:,}**")
    lines.append(f"• Alerts fired: **{total_alerts:,}**")
    lines.append("")
    lines.append("📈 Per-bot metrics:")
    for r in bot_rows_by_name:
        lines.append(
            f"• {r['name']}: "
            f"scanned={r['scanned']:,} | matches={r['matched']:,} | alerts={r['alerts']:,}"
        )

    # Top 3 heavy bots by scans (keep your existing vibe)
    top3 = bot_rows_by_scans[:3]
    if top3:
        lines.append("────────────────")
        lines.append("🥵 Top 3 Heaviest Bots (by scans):")
        for r in top3:
            lines.append(f"• {r['name']}: {r['scanned']:,} scanned")

    text = "\n".join(lines)

    _send_telegram_status(text)

    # Update heartbeat timestamp
    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)

    print("[status_report] Heartbeat sent.")