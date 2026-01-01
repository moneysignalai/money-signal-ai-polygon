# bots/status_report.py
from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from bots.shared import (
    STATS_PATH,
    format_est_timestamp,
    now_est,
    now_est_dt,
    record_bot_stats,
    today_est_date,
)

# ----------------- CONFIG -----------------

HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))
DEBUG_STATUS_PING_ENABLED = os.getenv("DEBUG_STATUS_PING_ENABLED", "false").lower() == "true"
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")
_TELEGRAM_STATUS_TOKEN = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS

# Primary bots shown in the heartbeat
BOT_DISPLAY_ORDER: List[str] = [
    "premarket",
    "volume_monster",
    "gap_flow",
    "swing_pullback",
    "trend_rider",
    "panic_flush",
    "momentum_reversal",
    "rsi_signals",
    "opening_range_breakout",
    "options_cheap_flow",
    "options_unusual_flow",
    "options_whales",
    "options_iv_crush",
    "options_indicator",
    "squeeze",
    "earnings",
    "dark_pool_radar",
    "daily_ideas",
]

DISPLAY_NAME_OVERRIDES = {
    "opening_range_breakout": "ORB",
    "rsi_signals": "RSI Signals",
    "dark_pool_radar": "Dark Pool",
}

RUNTIME_HISTORY_MAX = 20


# ----------------- MODELS -----------------


@dataclass
class BotRun:
    bot_name: str
    scanned: int
    matched: int
    alerts: int
    runtime: float
    finished_at_ts: float
    finished_at_str: str
    trading_day: str


@dataclass
class BotRow:
    internal_name: str
    display_name: str
    scanned: int
    matched: int
    alerts: int
    last_run_ts: float
    last_run_str: str
    runtime_history: List[float]


# ----------------- FILE I/O -----------------


def _load_stats() -> Dict[str, Any]:
    try:
        if not os.path.exists(STATS_PATH):
            return {"bots": {}, "errors": [], "last_heartbeat_ts": 0.0}
        with open(STATS_PATH, "r") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[status_report] failed to load stats from {STATS_PATH}: {exc}")
    return {"bots": {}, "errors": [], "last_heartbeat_ts": 0.0}


def _save_stats(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    except Exception:
        pass
    try:
        with open(STATS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[status_report] failed to save stats to {STATS_PATH}: {exc}")


# ----------------- ERROR RECORDING -----------------


def record_error(bot_name: str, exc: Exception) -> None:
    data = _load_stats()
    errors = data.get("errors", [])
    entry = {
        "ts": time.time(),
        "bot": bot_name,
        "type": exc.__class__.__name__,
        "msg": str(exc),
        "when": now_est(),
    }
    errors.append(entry)
    if len(errors) > 50:
        errors = errors[-50:]
    data["errors"] = errors
    _save_stats(data)
    print(f"[status_report] error recorded for {bot_name}: {exc}")


# ----------------- TELEGRAM HELPERS -----------------


def _send_telegram_status(text: str) -> None:
    if not _TELEGRAM_STATUS_TOKEN or not TELEGRAM_CHAT_ALL:
        print("[status_report] Telegram status token or chat ID not set; printing instead:")
        print(text)
        return

    try:
        url = f"https://api.telegram.org/bot{_TELEGRAM_STATUS_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ALL, "text": text, "parse_mode": "Markdown"}
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[status_report] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as exc:  # pragma: no cover
        print(f"[status_report] Telegram send error: {exc}")


# ----------------- NORMALIZATION -----------------


def _display_name(bot_name: str) -> str:
    if bot_name in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[bot_name]
    label = bot_name.replace("_", " ").strip()
    return label.title() if label else bot_name


def _pad_label(name: str, width: int = 18) -> str:
    """Return a dotted label padded for alignment."""

    clean = name.strip()
    if len(clean) >= width:
        return clean
    dots = "…" * max(2, width - len(clean))
    return f"{clean} {dots}"


def _normalize_runs(entry: Any) -> List[Dict[str, Any]]:
    """Return a list of run dicts for a bot, tolerating legacy formats."""

    runs: List[Dict[str, Any]] = []
    if not isinstance(entry, dict):
        return runs

    hist = entry.get("history")
    if isinstance(hist, list):
        runs.extend([r for r in hist if isinstance(r, dict)])

    latest = entry.get("latest")
    if isinstance(latest, dict):
        runs.append(latest)
    elif {"scanned", "matched", "alerts"}.issubset(entry.keys()):
        # Legacy flat entry
        legacy = dict(entry)
        if "trading_day" not in legacy:
            ts = float(legacy.get("last_run_ts", time.time()))
            dt = datetime.fromtimestamp(ts, tz=now_est_dt().tzinfo)
            legacy["trading_day"] = dt.date().isoformat()
            legacy["finished_at_ts"] = ts
            legacy["finished_at_str"] = legacy.get("last_run_str", format_est_timestamp(dt))
            legacy["runtime"] = float(legacy.get("last_runtime", 0.0))
        runs.append(legacy)

    return runs


def _aggregate_today(bot_name: str, entry: Any, today_iso: str) -> BotRow:
    runs = _normalize_runs(entry)
    today_runs = [r for r in runs if r.get("trading_day") == today_iso]

    scanned = sum(int(r.get("scanned", 0)) for r in today_runs)
    matched = sum(int(r.get("matched", 0)) for r in today_runs)
    alerts = sum(int(r.get("alerts", 0)) for r in today_runs)

    runtime_history = []
    for r in today_runs:
        try:
            runtime_history.append(float(r.get("runtime", 0.0)))
        except Exception:
            continue
    if len(runtime_history) > RUNTIME_HISTORY_MAX:
        runtime_history = runtime_history[-RUNTIME_HISTORY_MAX:]

    last_run_ts = 0.0
    last_run_str = ""
    if today_runs:
        newest = max(today_runs, key=lambda r: float(r.get("finished_at_ts", 0.0)))
        last_run_ts = float(newest.get("finished_at_ts", 0.0))
        last_run_str = newest.get("finished_at_str", "")

    return BotRow(
        internal_name=bot_name,
        display_name=_display_name(bot_name),
        scanned=scanned,
        matched=matched,
        alerts=alerts,
        last_run_ts=last_run_ts,
        last_run_str=last_run_str,
        runtime_history=runtime_history,
    )


# ----------------- HEARTBEAT FORMAT -----------------


def _format_heartbeat() -> str:
    data = _load_stats()
    bots_data: Dict[str, Any] = data.get("bots", {})
    errors_data: List[Dict[str, Any]] = data.get("errors", [])

    today_iso = today_est_date().isoformat()

    bot_rows: List[BotRow] = []
    total_scanned = total_matched = total_alerts = 0

    for internal in BOT_DISPLAY_ORDER:
        row = _aggregate_today(internal, bots_data.get(internal, {}), today_iso)
        bot_rows.append(row)
        total_scanned += row.scanned
        total_matched += row.matched
        total_alerts += row.alerts

    now_ts = time.time()
    recent_errors = [e for e in errors_data if now_ts - float(e.get("ts", 0.0)) <= 60 * 60]
    error_bots = {str(e.get("bot", "")).lower() for e in recent_errors}

    status_line = "✅ ALL SYSTEMS GOOD"
    if recent_errors:
        status_line = "⚠️ PARTIAL ISSUES" if len(recent_errors) < 3 else "❌ ERRORS DETECTED"

    lines: List[str] = []
    lines.append(f"📡 MoneySignalAI Heartbeat · {now_est()}")
    lines.append(status_line)
    lines.append("")

    # Bots section
    lines.append("🤖 Bots")
    for r in bot_rows:
        padded = _pad_label(r.display_name)
        if r.last_run_ts == 0:
            status = "⚪"
            last_seen = "No run today"
        else:
            status = "🟢"
            last_seen = r.last_run_str or "No run today"
            if r.scanned == 0:
                status = "🟠"
        if r.internal_name.lower() in error_bots:
            status = "🔴"
        lines.append(f"• {padded} {status} {last_seen}")

    # Totals
    lines.append("")
    lines.append("📊 Totals")
    lines.append(f"• Scanned: {total_scanned:,} • Matches: {total_matched:,} • Alerts: {total_alerts:,}")

    # Per-bot metrics
    lines.append("")
    lines.append("📈 Per Bot (scanned | matches | alerts)")
    for r in bot_rows:
        padded = _pad_label(r.display_name)
        lines.append(f"• {padded} {r.scanned:,} | {r.matched:,} | {r.alerts:,}")

    high_scan_zero_alert = [r.display_name for r in bot_rows if r.scanned > 0 and r.alerts == 0]
    ran_zero_scans = [r.display_name for r in bot_rows if r.last_run_ts > 0 and r.scanned == 0]
    not_run_today = [r.display_name for r in bot_rows if r.last_run_ts == 0]

    # Diagnostics
    lines.append("")
    lines.append("🛠 Diagnostics")
    lines.append(
        "• High scan, zero alerts: " + (", ".join(sorted(high_scan_zero_alert)) if high_scan_zero_alert else "none")
    )
    lines.append(
        "• Ran today, zero scans (check universes/filters): "
        + (", ".join(sorted(ran_zero_scans)) if ran_zero_scans else "none")
    )
    lines.append(
        "• Not run today: " + (", ".join(sorted(not_run_today)) if not_run_today else "none")
    )

    # Runtime summary (today only)
    lines.append("")
    lines.append("⏱ Runtime (today)")
    for r in bot_rows:
        padded = _pad_label(r.display_name)
        if not r.runtime_history:
            lines.append(f"• {padded} no runtime data yet")
            continue
        med = statistics.median(r.runtime_history)
        last = r.runtime_history[-1]
        lines.append(f"• {padded} median {med:.2f}s (last {last:.2f}s, n={len(r.runtime_history)})")

    return "\n".join(lines)


# ----------------- ENTRYPOINT -----------------


async def run_status() -> None:
    data = _load_stats()
    last_hb = float(data.get("last_heartbeat_ts", 0.0))
    now_ts = time.time()
    min_interval_sec = HEARTBEAT_INTERVAL_MIN * 60.0
    if now_ts - last_hb < min_interval_sec:
        if DEBUG_STATUS_PING_ENABLED:
            print(
                f"[status_report] Heartbeat skipped (interval). "
                f"since_last={now_ts - last_hb:.1f}s, min={min_interval_sec:.1f}s"
            )
        return

    text = _format_heartbeat()
    if DEBUG_STATUS_PING_ENABLED:
        print(f"[status_report] Sending heartbeat (len={len(text)} chars)")

    _send_telegram_status(text)
    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)
    print("[status_report] Heartbeat sent.")


# Backwards-compatible alias used by main.py health endpoint
load_stats = _load_stats
