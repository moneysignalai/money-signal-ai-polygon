# bots/status_report.py

import os
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Optional

from bots.shared import now_est  # reuse your EST timestamp helper

import requests

# Where we persist per-bot stats between scans
STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")

# Heartbeat minimum interval (minutes)
HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))

# Extra console logging for status behavior
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

# How many recent runtimes we keep per bot for median stats
RUNTIME_HISTORY_MAX = 20


@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    last_runtime: float = 0.0
    last_run_ts: float = 0.0
    last_run_str: str = ""
    # NEW: rolling history of runtimes (seconds) for latency stats
    runtime_samples: List[float] = field(default_factory=list)


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


def _update_runtime_history(
    existing: Optional[List[float]],
    new_runtime: float,
    max_len: int = RUNTIME_HISTORY_MAX,
) -> List[float]:
    """
    Append new runtime to history and keep only the last `max_len` entries.
    """
    history = list(existing or [])
    if new_runtime > 0:
        history.append(float(new_runtime))
    if len(history) > max_len:
        history = history[-max_len:]
    return history


def record_bot_stats(
    bot_name: str,
    scanned: int,
    matched: int,
    alerts: int,
    runtime: float,
) -> None:
    """
    Called by each bot at the end of its run.

    This updates the per-bot stats and preserves other bots' data,
    while maintaining a rolling runtime history for latency stats.
    """
    data = _load_stats()
    bots = data.get("bots", {})

    now_ts = time.time()
    pretty_ts = now_est()

    # Pull any existing runtime history for this bot
    existing_info = bots.get(bot_name, {})
    existing_history = existing_info.get("runtime_samples", [])

    runtime_samples = _update_runtime_history(existing_history, runtime)

    stats = BotStats(
        bot_name=bot_name,
        scanned=scanned,
        matched=matched,
        alerts=alerts,
        last_runtime=runtime,
        last_run_ts=now_ts,
        last_run_str=pretty_ts,
        runtime_samples=runtime_samples,
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


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    n = len(vs)
    mid = n // 2
    if n % 2 == 1:
        return vs[mid]
    return (vs[mid - 1] + vs[mid]) / 2.0


def _format_heartbeat() -> str:
    """
    Build the heartbeat message text.

    • Always lists all bots from BOT_DISPLAY_ORDER, even if they have no stats yet.
    • Shows per-bot "OK @ time" or "no recent run".
    • Shows global totals and per-bot scanned/matched/alerts.
    • Shows top 3 heaviest bots by scanned.
    • Includes recent errors (last 60 minutes) if any.
    • NEW: Includes per-bot latency stats (median + last runtime).
    """
    data = _load_stats()
    bots_data: Dict[str, Any] = data.get("bots", {})
    errors_data: List[Dict[str, Any]] = data.get("errors", [])

    # Build normalized rows for every bot in display order
    bot_rows: List[Dict[str, Any]] = []
    total_scanned = 0
    total_matched = 0
    total_alerts = 0

    for name in BOT_DISPLAY_ORDER:
        info = bots_data.get(name, {})
        scanned = int(info.get("scanned", 0))
        matched = int(info.get("matched", 0))
        alerts = int(info.get("alerts", 0))
        last_runtime = float(info.get("last_runtime", 0.0))
        last_run_str = info.get("last_run_str", "no recent run")
        last_run_ts = float(info.get("last_run_ts", 0.0))

        runtime_samples_raw = info.get("runtime_samples") or []
        runtime_samples = [float(x) for x in runtime_samples_raw if isinstance(x, (int, float, str))]

        median_runtime = _median(runtime_samples)

        total_scanned += scanned
        total_matched += matched
        total_alerts += alerts

        bot_rows.append(
            {
                "name": name,
                "scanned": scanned,
                "matched": matched,
                "alerts": alerts,
                "last_runtime": last_runtime,
                "last_run_str": last_run_str,
                "last_run_ts": last_run_ts,
                "runtime_samples": runtime_samples,
                "median_runtime": median_runtime,
            }
        )

    # Sort a copy by scanned volume for "heaviest" section
    bot_rows_by_scans = sorted(bot_rows, key=lambda r: r["scanned"], reverse=True)
    top3 = [r for r in bot_rows_by_scans if r["scanned"] > 0][:3]

    # Build message lines
    lines: List[str] = []
    lines.append("📡 *MoneySignalAI Heartbeat* ❤️")
    lines.append(f"⏰ {now_est()}")
    lines.append("────────────────")

    # System-level status
    lines.append("✅ ALL SYSTEMS GOOD")
    lines.append("")
    lines.append("🤖 *Bot Status:*")

    from bots.shared import is_bot_test_mode, is_bot_disabled  # safe small import

    for r in bot_rows:
        name = r["name"]
        last_run_ts = r["last_run_ts"]
        last_run_str = r["last_run_str"]

        # Mark test-mode / disabled bots
        name_display = name
        if is_bot_disabled(name):
            name_display = f"{name} (DISABLED)"
        elif is_bot_test_mode(name):
            name_display = f"{name} (TEST)"

        if last_run_ts > 0:
            lines.append(f"• ✅ {name_display}: OK @ {last_run_str}")
        else:
            lines.append(f"• ❔ {name_display}: no recent run")

    lines.append("────────────────")
    lines.append("📊 *Scanner Analytics:*")
    lines.append(f"• Total scanned: **{total_scanned:,}**")
    lines.append(f"• Filter matches: **{total_matched:,}**")
    lines.append(f"• Alerts fired: **{total_alerts:,}**")
    lines.append("")

    lines.append("📈 *Per-bot metrics:*")
    for r in bot_rows:
        lines.append(
            f"• {r['name']}: scanned={r['scanned']:,} | "
            f"matches={r['matched']:,} | alerts={r['alerts']:,}"
        )

    # NEW: latency / runtime section
    lines.append("────────────────")
    lines.append("⏱ *Runtime (per bot):*")
    # sort by median runtime, descending, so hogs float to top
    by_latency = sorted(bot_rows, key=lambda r: r["median_runtime"], reverse=True)
    for r in by_latency:
        med = r["median_runtime"]
        last = r["last_runtime"]
        count = len(r["runtime_samples"])
        if count == 0 and last == 0:
            lines.append(f"• {r['name']}: no runtime data yet")
        else:
            # Example: median 12.3s (last 10.8s, n=7)
            lines.append(
                f"• {r['name']}: median {med:.2f}s "
                f"(last {last:.2f}s, n={count})"
            )

    # Bots that are scanning a lot but never alert (helps you tune filters)
    high_scan_low_alert = [
        r for r in bot_rows
        if r["scanned"] >= 200 and r["alerts"] == 0
    ]
    if high_scan_low_alert:
        lines.append("────────────────")
        lines.append("🧐 *High-scan, zero-alert bots (tune filters?):*")
        for r in high_scan_low_alert:
            lines.append(
                f"• {r['name']}: scanned={r['scanned']:,}, "
                f"matches={r['matched']:,}, alerts={r['alerts']:,}"
            )

    if top3:
        lines.append("────────────────")
        lines.append("🥵 *Top 3 Heaviest Bots* (by scans):")
        for r in top3:
            lines.append(f"• {r['name']}: {r['scanned']:,} scanned")

    # Optional: recent errors within last 60 minutes
    now_ts = time.time()
    recent_errors = [
        e for e in errors_data
        if now_ts - float(e.get("ts", 0.0)) <= 60 * 60
    ]
    if recent_errors:
        lines.append("────────────────")
        lines.append("⚠️ *Recent Errors (last 60 min):*")
        for e in recent_errors[-5:]:  # show up to last 5
            bot = e.get("bot", "?")
            when = e.get("when", "?")
            etype = e.get("type", "?")
            msg = e.get("msg", "")
            # keep error lines short-ish
            if len(msg) > 120:
                msg = msg[:117] + "..."
            lines.append(f"• {bot} ({etype}) @ {when} → {msg}")

    return "\n".join(lines)


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
    delta = now_ts - last_hb

    if delta < min_interval_sec and last_hb > 0:
        if DEBUG_STATUS_PING_ENABLED:
            print(
                f"[status_report] Heartbeat skipped (interval). "
                f"elapsed={delta:.1f}s, required={min_interval_sec:.1f}s"
            )
        return

    if DEBUG_STATUS_PING_ENABLED:
        print("[status_report] Building heartbeat message...")

    text = _format_heartbeat()
    _send_telegram_status(text)

    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)

    print("[status_report] Heartbeat sent.")