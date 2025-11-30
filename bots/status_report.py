# bots/status_report.py

import os
import json
import time
import statistics
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List, Tuple

from bots.shared import now_est  # reuse your EST timestamp helper

import requests

# ---------------- CONFIG ----------------

# Where we persist per-bot stats between scans
STATS_PATH = os.getenv("STATUS_STATS_PATH", "/tmp/moneysignal_stats.json")

# Heartbeat minimum interval (minutes)
HEARTBEAT_INTERVAL_MIN = float(os.getenv("STATUS_HEARTBEAT_INTERVAL_MIN", "5"))

# How many recent runtimes to keep per bot for median calc
RUNTIME_HISTORY_DEPTH = int(os.getenv("STATUS_RUNTIME_HISTORY_DEPTH", "20"))

# Optional extra console noise for debugging heartbeat behavior
DEBUG_STATUS_PING_ENABLED = os.getenv("DEBUG_STATUS_PING_ENABLED", "false").lower() == "true"

# Telegram routing (reuse same envs you already use)
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL")
TELEGRAM_TOKEN_STATUS = os.getenv("TELEGRAM_TOKEN_STATUS")
TELEGRAM_TOKEN_ALERTS = os.getenv("TELEGRAM_TOKEN_ALERTS")

# If status token not set, fall back to alerts token
_TELEGRAM_STATUS_TOKEN = TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_ALERTS

# Human-friendly display order (bot keys must match what main.py / bots use)
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


# ---------------- DATA MODEL ----------------

@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    last_runtime: float = 0.0
    last_run_ts: float = 0.0
    last_run_str: str = ""
    # recent runtimes for median, stored newest-last
    runtime_history: List[float] = field(default_factory=list)


# ---------------- STORAGE HELPERS ----------------

def _load_stats() -> Dict[str, Any]:
    """Load stats JSON from disk."""
    try:
        if not os.path.exists(STATS_PATH):
            if DEBUG_STATUS_PING_ENABLED:
                print(f"[status_report] STATS_PATH does not exist yet: {STATS_PATH}")
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


# ---------------- RECORDING FROM BOTS ----------------

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
    Also maintains a rolling history of runtimes for median calculations.
    """
    data = _load_stats()
    bots = data.get("bots", {})

    now_ts = time.time()
    pretty_ts = now_est()

    # Start from any existing entry to preserve history
    existing = bots.get(bot_name, {})
    existing_history = existing.get("runtime_history", []) or []

    # Append latest runtime, keep only last N
    try:
        existing_history = list(existing_history)
    except Exception:
        existing_history = []
    existing_history.append(float(runtime))
    if len(existing_history) > RUNTIME_HISTORY_DEPTH:
        existing_history = existing_history[-RUNTIME_HISTORY_DEPTH:]

    stats = BotStats(
        bot_name=bot_name,
        scanned=scanned,
        matched=matched,
        alerts=alerts,
        last_runtime=runtime,
        last_run_ts=now_ts,
        last_run_str=pretty_ts,
        runtime_history=existing_history,
    )
    bots[bot_name] = asdict(stats)
    data["bots"] = bots

    _save_stats(data)
    if DEBUG_STATUS_PING_ENABLED:
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


# ---------------- TELEGRAM SENDING ----------------

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
            # Use basic Markdown, keep dynamic content outside of *...* to avoid parse issues
            "parse_mode": "Markdown",
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[status_report] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[status_report] Telegram send error: {e}")


# ---------------- PRETTY LABEL HELPERS ----------------

def _pretty_bot_label(name: str) -> str:
    """
    Turn internal bot keys into nicer labels for display.

    equity_flow -> Equity Flow
    rsi_signals -> RSI Signals
    dark_pool_radar -> Dark Pool Radar
    """
    base = name.replace("_", " ").strip()
    if not base:
        return name

    # Simple title-case then fix known acronyms
    title = base.title()
    # Fix specific cases
    title = title.replace("Rsi", "RSI")
    title = title.replace("Etf", "ETF")
    title = title.replace("Orb", "ORB")
    return title


def _normalize_bot_row(name: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure we always have all fields present for a given bot."""
    scanned = int(info.get("scanned", 0) or 0)
    matched = int(info.get("matched", 0) or 0)
    alerts = int(info.get("alerts", 0) or 0)
    last_runtime = float(info.get("last_runtime", 0.0) or 0.0)
    last_run_ts = float(info.get("last_run_ts", 0.0) or 0.0)
    last_run_str = info.get("last_run_str", "no recent run")

    rh = info.get("runtime_history", []) or []
    try:
        runtime_history = [float(x) for x in rh]
    except Exception:
        runtime_history = []

    return {
        "name": name,
        "label": _pretty_bot_label(name),
        "scanned": scanned,
        "matched": matched,
        "alerts": alerts,
        "last_runtime": last_runtime,
        "last_run_ts": last_run_ts,
        "last_run_str": last_run_str,
        "runtime_history": runtime_history,
    }


# ---------------- HEARTBEAT FORMATTING ----------------

def _format_heartbeat() -> str:
    """
    Build the heartbeat message text.

    • Always lists all bots from BOT_DISPLAY_ORDER, even if they have no stats yet.
    • Shows per-bot "OK @ time" or "no recent run".
    • Shows global totals and per-bot scanned/matched/alerts.
    • Shows latency metrics (median / last / n).
    • Shows top 3 heaviest bots by scanned.
    • Optionally includes recent errors.
    """
    data = _load_stats()
    bots_data: Dict[str, Any] = data.get("bots", {})
    errors_data: List[Dict[str, Any]] = data.get("errors", [])

    # Normalize rows for every known bot
    bot_rows: List[Dict[str, Any]] = []
    total_scanned = 0
    total_matched = 0
    total_alerts = 0

    # Ensure we include anything that might not be in BOT_DISPLAY_ORDER yet
    all_bot_names: List[str] = list(BOT_DISPLAY_ORDER)
    for k in bots_data.keys():
        if k not in all_bot_names:
            all_bot_names.append(k)

    for name in all_bot_names:
        info = bots_data.get(name, {})
        row = _normalize_bot_row(name, info)

        total_scanned += row["scanned"]
        total_matched += row["matched"]
        total_alerts += row["alerts"]

        bot_rows.append(row)

    # Sort copies for heaviest + runtime sections
    bot_rows_by_scans = sorted(bot_rows, key=lambda r: r["scanned"], reverse=True)
    top3 = [r for r in bot_rows_by_scans if r["scanned"] > 0][:3]

    # Runtime stats, sorted by median runtime (desc)
    runtime_rows: List[Tuple[str, str, float, float, int]] = []
    for r in bot_rows:
        history = r["runtime_history"]
        if history:
            try:
                median_rt = statistics.median(history)
            except statistics.StatisticsError:
                median_rt = history[-1]
            last_rt = history[-1]
            n = len(history)
            runtime_rows.append((r["name"], r["label"], median_rt, last_rt, n))

    runtime_rows.sort(key=lambda x: x[2], reverse=True)  # sort by median_rt desc

    # Build message lines
    lines: List[str] = []
    lines.append("📡 *MoneySignalAI Heartbeat* ❤️")
    lines.append(f"⏰ {now_est()}")
    lines.append("────────────────")
    lines.append("✅ *System:* ALL SYSTEMS GOOD")
    lines.append("")

    # Bot status (OK / no recent run), including test/disabled flags
    lines.append("🤖 *Bot Status:*")

    from bots.shared import is_bot_test_mode, is_bot_disabled  # small safe import

    for r in bot_rows:
        name = r["name"]
        label = r["label"]
        last_run_ts = r["last_run_ts"]
        last_run_str = r["last_run_str"]

        display = label
        if is_bot_disabled(name):
            display = f"{label} (DISABLED)"
        elif is_bot_test_mode(name):
            display = f"{label} (TEST)"

        if last_run_ts > 0:
            lines.append(f"• ✅ {display}: OK @ {last_run_str}")
        else:
            lines.append(f"• ❔ {display}: no recent run")

    # Global scanner analytics
    lines.append("────────────────")
    lines.append("📊 *Scanner Analytics:*")
    lines.append(f"• Total scanned: **{total_scanned:,}**")
    lines.append(f"• Filter matches: **{total_matched:,}**")
    lines.append(f"• Alerts fired: **{total_alerts:,}**")
    lines.append("")

    # Per-bot metrics (raw numbers)
    lines.append("📈 *Per-bot metrics:*")
    for r in bot_rows:
        # Keep this section simple (no Markdown entities inside bot names)
        lines.append(
            f"• {r['name']}: scanned={r['scanned']:,} | "
            f"matches={r['matched']:,} | alerts={r['alerts']:,}"
        )

    # Runtime metrics
    lines.append("────────────────")
    lines.append("⏱ *Runtime (per bot, last runs):*")
    if runtime_rows:
        for name, label, median_rt, last_rt, n in runtime_rows:
            lines.append(
                f"• {label}: median {median_rt:.2f}s "
                f"(last {last_rt:.2f}s, n={n})"
            )
    else:
        lines.append("• No runtime data yet")

    # Bots that scan a lot but never alert (tuning candidates)
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

    # Top 3 by scans
    if top3:
        lines.append("────────────────")
        lines.append("🥵 *Top 3 Heaviest Bots* (by scans):")
        max_scans = max(r["scanned"] for r in top3) or 1
        for r in top3:
            # tiny ASCII bar based on relative scans
            bar_len = max(1, int(5 * r["scanned"] / max_scans))
            bar = "▇" * bar_len
            lines.append(f"• {r['name']}: {r['scanned']:,} scanned {bar}")

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


# ---------------- HEARTBEAT ENTRYPOINT ----------------

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

    if DEBUG_STATUS_PING_ENABLED:
        print(
            f"[status_report] run_status called. "
            f"delta_since_last={delta:.1f}s, "
            f"min_interval={min_interval_sec:.1f}s"
        )

    if delta < min_interval_sec:
        if DEBUG_STATUS_PING_ENABLED:
            print("[status_report] Heartbeat skipped (interval not reached).")
        return

    text = _format_heartbeat()
    _send_telegram_status(text)

    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)

    print("[status_report] Heartbeat sent.")