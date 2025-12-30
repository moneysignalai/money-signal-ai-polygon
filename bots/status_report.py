# bots/status_report.py

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import requests

from bots.shared import now_est, is_bot_disabled, is_bot_test_mode  # reuse existing helpers

# ----------------- CONFIG -----------------

# Where we persist per-bot stats between scans
STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")

# Heartbeat minimum interval (minutes)
HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))

# Optional extra logging for heartbeat decisions
DEBUG_STATUS_PING_ENABLED = os.getenv("DEBUG_STATUS_PING_ENABLED", "false").lower() == "true"

# Telegram routing (reuse same envs you already use)
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")

# If status token not set, fall back to alerts token
_TELEGRAM_STATUS_TOKEN = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS

# Human-friendly display order (must match bot names used in main.py / bots)
BOT_DISPLAY_ORDER: List[str] = [
    "premarket",
    "equity_flow",
    "intraday_flow",
    "rsi_signals",
    "opening_range_breakout",
    "options_flow",
    "options_indicator",
    "squeeze",
    "earnings",
    "trend_flow",
    "dark_pool_radar",
    "daily_ideas",
]

# How many runtimes to keep in rolling history (for median)
RUNTIME_HISTORY_MAX = 20


# ----------------- MODELS -----------------


@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    last_runtime: float = 0.0
    last_run_ts: float = 0.0
    last_run_str: str = ""
    runtime_history: List[float] = field(default_factory=list)


# ----------------- FILE I/O -----------------


def _load_stats() -> Dict[str, Any]:
    """Load stats JSON from disk."""
    try:
        if not os.path.exists(STATS_PATH):
            return {"bots": {}, "errors": [], "last_heartbeat_ts": 0.0}
        with open(STATS_PATH, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[status_report] failed to load stats from {STATS_PATH}: {e}")
        return {"bots": {}, "errors": [], "last_heartbeat_ts": 0.0}


def _save_stats(data: Dict[str, Any]) -> None:
    """Persist stats JSON to disk."""
    try:
        os.makedirs(os.path.dirname(STATS_PATH), exist_ok=True)
    except Exception:
        pass

    try:
        with open(STATS_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[status_report] failed to save stats to {STATS_PATH}: {e}")


# ----------------- RECORDING HELPERS -----------------


def record_bot_stats(
    bot_name: str,
    scanned: int,
    matched: int,
    alerts: int,
    runtime: float,
) -> None:
    """
    Called by each bot at the end of its run.

    This updates the per-bot stats and preserves other bots' data.
    It also maintains a small rolling history of runtimes so we can
    compute per-bot median runtime in the heartbeat.
    """
    data = _load_stats()
    bots = data.get("bots", {})

    now_ts = time.time()
    pretty_ts = now_est()

    prev = bots.get(bot_name) or {}
    prev_history = prev.get("runtime_history") or []

    # Normalize history into list of floats
    history: List[float] = []
    if isinstance(prev_history, list):
        for x in prev_history:
            try:
                history.append(float(x))
            except Exception:
                continue

    # Append new runtime, trim to max length
    try:
        history.append(float(runtime))
    except Exception:
        pass
    if len(history) > RUNTIME_HISTORY_MAX:
        history = history[-RUNTIME_HISTORY_MAX:]

    stats = BotStats(
        bot_name=bot_name,
        scanned=int(scanned),
        matched=int(matched),
        alerts=int(alerts),
        last_runtime=float(runtime),
        last_run_ts=now_ts,
        last_run_str=pretty_ts,
        runtime_history=history,
    )
    bots[bot_name] = asdict(stats)
    data["bots"] = bots

    _save_stats(data)
    print(
        f"[status_report] stats recorded for {bot_name}: "
        f"scanned={scanned} matched={matched} alerts={alerts} runtime={runtime:.2f}s"
    )


def record_error(bot_name: str, exc: Exception) -> None:
    """
    Optional error logger used by main.py when a bot throws.

    We store a rolling list of recent errors, which can be surfaced in the heartbeat.
    """
    data = _load_stats()
    errors = data.get("errors", [])

    now_ts = time.time()
    err_entry = {
        "ts": now_ts,
        "bot": bot_name,
        "type": exc.__class__.__name__,
        "msg": str(exc),
        "when": now_est(),
    }
    errors.append(err_entry)

    # Keep only the last N errors to avoid unbounded growth
    MAX_ERRORS = 50
    if len(errors) > MAX_ERRORS:
        errors = errors[-MAX_ERRORS:]

    data["errors"] = errors
    _save_stats(data)

    print(f"[status_report] error recorded for {bot_name}: {exc}")


# ----------------- TELEGRAM HELPERS -----------------


def _escape_markdown(text: str) -> str:
    """
    Minimal escaping for Telegram Markdown.

    We only escape characters that are likely to break formatting when they
    appear inside dynamic content such as error messages.
    """
    if not text:
        return text
    for ch in ("*", "_", "[", "]", "(", ")", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


def _send_telegram_status(text: str) -> None:
    """Send heartbeat/status text to Telegram, if configured."""
    if not _TELEGRAM_STATUS_TOKEN or not TELEGRAM_CHAT_ALL:
        print("[status_report] Telegram status token or chat ID not set; printing instead:")
        print(text)
        return

    try:
        url = f"https://api.telegram.org/bot{_TELEGRAM_STATUS_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ALL,
            "text": text,
            "parse_mode": "Markdown",
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[status_report] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[status_report] Telegram send error: {e}")


# ----------------- HEARTBEAT FORMAT -----------------


DISPLAY_NAME_OVERRIDES = {
    "opening_range_breakout": "ORB",
    "rsi_signals": "RSI Signals",
    "dark_pool_radar": "Dark Pool",
    "options_indicator": "Options Ind",
}


def _display_name(bot_name: str) -> str:
    """
    Convert internal bot name (equity_flow) to a human-readable label ("Equity Flow").
    Applies short aliases where helpful.
    """
    if bot_name in DISPLAY_NAME_OVERRIDES:
        return DISPLAY_NAME_OVERRIDES[bot_name]
    label = bot_name.replace("_", " ").strip()
    if not label:
        return bot_name
    return label.title()


def _pad_label(label: str, width: int = 13) -> str:
    """Pad a label with ellipsis characters to align columns."""
    if len(label) >= width:
        return label + " "
    fill = "…" * (width - len(label))
    return f"{label} {fill} "


def _short_time(ts: str) -> str:
    """Return the HH:MM AM/PM part from a human-friendly timestamp string."""
    if not ts:
        return ts
    if "·" in ts:
        return ts.split("·", 1)[0].strip()
    return ts


def _format_heartbeat() -> str:
    """
    Build the heartbeat message text in a compact, sectioned layout.
    """
    data = _load_stats()
    bots_data: Dict[str, Any] = data.get("bots", {})
    errors_data: List[Dict[str, Any]] = data.get("errors", [])

    # Build normalized rows for every bot in display order
    bot_rows: List[Dict[str, Any]] = []
    total_scanned = 0
    total_matched = 0
    total_alerts = 0

    for internal_name in BOT_DISPLAY_ORDER:
        info = bots_data.get(internal_name, {})
        scanned = int(info.get("scanned", 0))
        matched = int(info.get("matched", 0))
        alerts = int(info.get("alerts", 0))
        last_runtime = float(info.get("last_runtime", 0.0))
        last_run_str = info.get("last_run_str", "no recent run")
        last_run_ts = float(info.get("last_run_ts", 0.0))

        rh = info.get("runtime_history") or []
        runtime_history: List[float] = []
        if isinstance(rh, list):
            for x in rh:
                try:
                    runtime_history.append(float(x))
                except Exception:
                    continue

        # Totals
        total_scanned += scanned
        total_matched += matched
        total_alerts += alerts

        bot_rows.append(
            {
                "internal_name": internal_name,
                "display_name": _display_name(internal_name),
                "scanned": scanned,
                "matched": matched,
                "alerts": alerts,
                "last_runtime": last_runtime,
                "last_run_str": last_run_str,
                "last_run_ts": last_run_ts,
                "runtime_history": runtime_history,
            }
        )

    now_ts = time.time()
    recent_errors = [
        e for e in errors_data
        if now_ts - float(e.get("ts", 0.0)) <= 60 * 60
    ]
    error_bots = {str(e.get("bot", "")).lower() for e in recent_errors}

    # Overall status line
    status_line = "✅ ALL SYSTEMS GOOD"
    if recent_errors:
        status_line = "⚠️ PARTIAL ISSUES" if len(recent_errors) < 3 else "❌ ERRORS DETECTED"

    lines: List[str] = []

    # Header
    lines.append(f"📡 MoneySignalAI Heartbeat · {now_est()}")
    lines.append(status_line)
    lines.append("")

    # Bot overview
    lines.append("🤖 Bots")
    for r in bot_rows:
        internal = r["internal_name"]
        display = _pad_label(r["display_name"])
        last_run_ts = r["last_run_ts"]
        last_run_str = r["last_run_str"] or "no recent run"

        status = "⚪" if last_run_ts <= 0 else "🟢"
        if internal.lower() in error_bots:
            status = "🔴"

        last_seen = _short_time(last_run_str) if last_run_ts > 0 else "No recent run"
        lines.append(f"• {display}{status} {last_seen}")

    # Totals
    lines.append("")
    lines.append("📊 Totals")
    totals_line = f"• Scanned: {total_scanned:,} • Matches: {total_matched:,} • Alerts: {total_alerts:,}"
    lines.append(totals_line)

    # Per-bot metrics
    lines.append("")
    lines.append("📈 Per Bot (scanned | matches | alerts)")
    for r in bot_rows:
        display = _pad_label(r["display_name"])
        scanned = r.get("scanned", "n/a")
        matched = r.get("matched", "n/a")
        alerts = r.get("alerts", "n/a")
        try:
            scanned_str = f"{int(scanned):,}"
        except Exception:
            scanned_str = "n/a"
        try:
            matched_str = f"{int(matched):,}"
        except Exception:
            matched_str = "n/a"
        try:
            alerts_str = f"{int(alerts):,}"
        except Exception:
            alerts_str = "n/a"
        lines.append(f"• {display}{scanned_str} | {matched_str} | {alerts_str}")

    # Diagnostics
    high_scan_zero_alert = [
        _display_name(r["internal_name"])
        for r in bot_rows
        if r.get("scanned", 0) > 0 and r.get("alerts", 0) == 0
    ]
    no_runtime = [
        _display_name(r["internal_name"])
        for r in bot_rows
        if float(r.get("last_run_ts", 0.0)) <= 0.0
    ]

    lines.append("")
    lines.append("🛠 Diagnostics")
    if high_scan_zero_alert:
        lines.append(
            "• High scan, zero alerts: " + ", ".join(sorted(high_scan_zero_alert))
        )
    else:
        lines.append("• High scan, zero alerts: none")

    if no_runtime:
        lines.append("• No runtime yet: " + ", ".join(sorted(no_runtime)))
    else:
        lines.append("• No runtime yet: none")

    return "\n".join(lines)


# ----------------- ENTRYPOINT -----------------


async def run_status() -> None:
    """
    Async entrypoint used by main.py each scheduler cycle.

    Applies a minimum interval between heartbeats and sends a Telegram
    status message when it's time.
    """
    data = _load_stats()
    last_hb = float(data.get("last_heartbeat_ts", 0.0))
    now_ts = time.time()

    min_interval_sec = HEARTBEAT_INTERVAL_MIN * 60.0
    since_last = now_ts - last_hb

    if since_last < min_interval_sec:
        if DEBUG_STATUS_PING_ENABLED:
            print(
                f"[status_report] Heartbeat skipped (interval). "
                f"since_last={since_last:.1f}s, min={min_interval_sec:.1f}s"
            )
        return

    text = _format_heartbeat()
    if DEBUG_STATUS_PING_ENABLED:
        print(
            f"[status_report] Sending heartbeat "
            f"(len={len(text)} chars, interval={HEARTBEAT_INTERVAL_MIN}m)"
        )

    _send_telegram_status(text)

    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)

    print("[status_report] Heartbeat sent.")