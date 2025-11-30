# bots/status_report.py

import os
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, Any, List

from bots.shared import now_est, is_bot_test_mode, is_bot_disabled  # reuse helpers
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


@dataclass
class BotStats:
    bot_name: str
    scanned: int = 0
    matched: int = 0
    alerts: int = 0
    last_runtime: float = 0.0
    last_run_ts: float = 0.0
    last_run_str: str = ""
    # keep a small rolling window of runtimes for median latency
    runtimes: List[float] = field(default_factory=list)


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
    Also maintains a rolling window of runtimes for latency medians.
    """
    data = _load_stats()
    bots = data.get("bots", {})

    now_ts = time.time()
    pretty_ts = now_est()

    # Merge with existing runtimes if present
    existing = bots.get(bot_name, {})
    runtimes = existing.get("runtimes", [])
    if not isinstance(runtimes, list):
        runtimes = []

    # Append current runtime and keep last N runs
    MAX_RUNTIME_SAMPLES = 20
    runtimes.append(float(runtime))
    if len(runtimes) > MAX_RUNTIME_SAMPLES:
        runtimes = runtimes[-MAX_RUNTIME_SAMPLES:]

    stats = BotStats(
        bot_name=bot_name,
        scanned=scanned,
        matched=matched,
        alerts=alerts,
        last_runtime=runtime,
        last_run_ts=now_ts,
        last_run_str=pretty_ts,
        runtimes=runtimes,
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
            "parse_mode": "Markdown",  # headings use *bold*, counts use **123**
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[status_report] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[status_report] Telegram send error: {e}")


def _median_runtime(runtimes: List[float]) -> float:
    """Return median of runtimes list (seconds). 0.0 if empty."""
    if not runtimes:
        return 0.0
    vals = sorted(float(x) for x in runtimes if isinstance(x, (int, float)))
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    if n % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _format_heartbeat() -> str:
    """
    Build the heartbeat message text.

    Structure:
      • Header + system line
      • Bot status + metrics in ONE section (no duplication)
      • Scanner totals
      • Top 3 heaviest bots by scans
      • Top 3 slowest bots by median runtime
      • Recent errors (last 60 minutes)
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
        runtimes = info.get("runtimes", [])
        if not isinstance(runtimes, list):
            runtimes = []

        med_runtime = _median_runtime(runtimes)

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
                "median_runtime": med_runtime,
                "last_run_str": last_run_str,
                "last_run_ts": last_run_ts,
            }
        )

    # Sort copies for rankings
    bot_rows_by_scans = sorted(bot_rows, key=lambda r: r["scanned"], reverse=True)
    top3_scans = [r for r in bot_rows_by_scans if r["scanned"] > 0][:3]

    bot_rows_by_latency = sorted(
        [r for r in bot_rows if r["median_runtime"] > 0],
        key=lambda r: r["median_runtime"],
        reverse=True,
    )
    top3_slowest = bot_rows_by_latency[:3]

    # Split into active vs no-run
    active_bots = [r for r in bot_rows if r["last_run_ts"] > 0]
    dormant_bots = [r for r in bot_rows if r["last_run_ts"] == 0]

    # Build message lines
    lines: List[str] = []
    lines.append("📡 *MoneySignalAI Heartbeat* ❤️")
    lines.append(f"⏰ {now_est()}")
    lines.append("────────────────")
    lines.append("✅ ALL SYSTEMS GOOD")
    lines.append("")

    # ---------------- BOT STATUS + METRICS ----------------
    lines.append("🤖 *Bot Status & Metrics:*")

    # Active bots (with last run)
    for r in active_bots:
        name = r["name"]
        scanned = r["scanned"]
        matched = r["matched"]
        alerts = r["alerts"]
        last_run_str = r["last_run_str"]
        last_runtime = r["last_runtime"]
        med_runtime = r["median_runtime"]

        # Add TEST / DISABLED tags based on shared.py helpers
        name_display = name
        if is_bot_disabled(name):
            name_display = f"{name} (DISABLED)"
        elif is_bot_test_mode(name):
            name_display = f"{name} (TEST)"

        if med_runtime > 0:
            rt_str = f"med {med_runtime:.1f}s"
        elif last_runtime > 0:
            rt_str = f"last {last_runtime:.1f}s"
        else:
            rt_str = "runtime N/A"

        lines.append(
            f"• ✅ {name_display}: {last_run_str} — "
            f"scanned={scanned:,} | matches={matched:,} | alerts={alerts:,} | {rt_str}"
        )

    # Bots that haven't run yet
    for r in dormant_bots:
        name = r["name"]
        name_display = name
        if is_bot_disabled(name):
            name_display = f"{name} (DISABLED)"
        elif is_bot_test_mode(name):
            name_display = f"{name} (TEST)"

        lines.append(f"• ❔ {name_display}: no recent run")

    # ---------------- SCANNER TOTALS ----------------
    lines.append("────────────────")
    lines.append("📊 *Scanner Analytics:*")
    lines.append(f"• Total scanned: **{total_scanned:,}**")
    lines.append(f"• Filter matches: **{total_matched:,}**")
    lines.append(f"• Alerts fired: **{total_alerts:,}**")

    # ---------------- HEAVIEST BOTS (BY SCANS) ----------------
    if top3_scans:
        lines.append("────────────────")
        lines.append("🥵 *Top 3 Heaviest Bots* (by scans):")
        max_scans = max(r["scanned"] for r in top3_scans)
        for r in top3_scans:
            name = r["name"]
            scanned = r["scanned"]
            # simple bar: 1–5 blocks relative to max
            bar_len = 1
            if max_scans > 0:
                bar_len = max(1, int(round((scanned / max_scans) * 5)))
            bar = "▇" * bar_len
            lines.append(f"• {name}: {scanned:,} scanned {bar}")

    # ---------------- SLOWEST BOTS (BY MEDIAN RUNTIME) ----------------
    if top3_slowest:
        lines.append("────────────────")
        lines.append("🐢 *Top 3 Slowest Bots* (by median runtime):")
        for r in top3_slowest:
            name = r["name"]
            med_runtime = r["median_runtime"]
            last_runtime = r["last_runtime"]
            lines.append(
                f"• {name}: median={med_runtime:.1f}s | last={last_runtime:.1f}s"
            )

    # ---------------- RECENT ERRORS ----------------
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
    if now_ts - last_hb < min_interval_sec:
        print("[status_report] Heartbeat skipped (interval).")
        return

    text = _format_heartbeat()
    _send_telegram_status(text)

    data["last_heartbeat_ts"] = now_ts
    _save_stats(data)

    print("[status_report] Heartbeat sent.")