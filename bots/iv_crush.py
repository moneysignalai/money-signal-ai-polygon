# bots/iv_crush.py

import os
import math
import time
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from polygon import RESTClient  # or massive.RESTClient if you've migrated

# ───────────────────────────────
# ENV & CONSTANTS
# ───────────────────────────────

POLYGON_KEY = os.getenv("POLYGON_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN_ALERTS", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ALL", "")

# Universe source:
#   1) If you already have a static CSV/JSON of tickers, use that.
#   2) Otherwise, we fall back to a "most active" style universe
#      using /v2/snapshot/locale/us/markets/stocks/mostactive
#      filtered down.
MOST_ACTIVE_URL = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/mostactive"

# IV Crush filters (tune as needed)
IVC_MAX_DTE = int(os.getenv("IVC_MAX_DTE", "7"))              # max days to expiry
IVC_MIN_IV = float(os.getenv("IVC_MIN_IV", "0.6"))            # 0.6 = 60% IV
IVC_MIN_OPTION_VOLUME = int(os.getenv("IVC_MIN_OPTION_VOLUME", "500"))
IVC_MIN_OPTION_OI = int(os.getenv("IVC_MIN_OPTION_OI", "200"))
IVC_MIN_UNDERLYING_PRICE = float(os.getenv("IVC_MIN_UNDERLYING_PRICE", "5.0"))
IVC_MAX_UNDERLYING_PRICE = float(os.getenv("IVC_MAX_UNDERLYING_PRICE", "250.0"))

# Implied vs actual move logic:
# IV is annualized; implied move over DTE days ≈ iv * sqrt(DTE / 365)
# We'll convert that to percentage and compare to today's actual move.
IVC_MIN_IMPLIED_MOVE_PCT = float(os.getenv("IVC_MIN_IMPLIED_MOVE_PCT", "8.0"))  # e.g. at least 8% move priced in
IVC_MAX_REALIZED_TO_IMPLIED_RATIO = float(os.getenv("IVC_MAX_REALIZED_TO_IMPLIED_RATIO", "0.55"))
# i.e. |realized move| <= 55% of implied move → "IV crush candidate"

# Telegram formatting
TELEGRAM_API_BASE = "https://api.telegram.org"

logger = logging.getLogger("iv_crush")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ───────────────────────────────
# Helpers
# ───────────────────────────────

def now_est_str() -> str:
    """Human-readable EST timestamp for alerts."""
    # Polygon data is US-focused; we label in EST for traders.
    est = datetime.now(timezone.utc).astimezone()
    return est.strftime("%I:%M %p %Z · %b %d")


def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram env vars missing; printing instead:\n%s", message)
        print(message)
        return

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        resp = requests.post(url, data=payload, timeout=10)
        if not resp.ok:
            logger.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)


def get_most_active_universe(limit: int = 100) -> List[str]:
    """
    Simple universe: top 'limit' most active US stocks today.
    Uses v2 snapshot endpoint. This is independent of your other bots'
    universe logic, so it won't interfere with them.
    """
    params = {"apiKey": POLYGON_KEY}
    try:
        r = requests.get(MOST_ACTIVE_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        tickers = [x["ticker"] for x in data.get("tickers", []) if "ticker" in x]
        return tickers[:limit]
    except Exception as e:
        logger.exception("Failed to fetch most active universe: %s", e)
        return []


def days_to_expiry(expiration: str) -> int:
    """
    expiration: 'YYYY-MM-DD'
    """
    try:
        exp = datetime.strptime(expiration, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        return (exp - today).days
    except Exception:
        return 9999


def estimate_implied_move_pct(iv: float, dte: int) -> float:
    """
    Very rough estimate of implied move between now and expiry in %.
    iv is annualized (e.g. 0.8 = 80%).
    """
    if iv <= 0 or dte <= 0:
        return 0.0
    return iv * math.sqrt(dte / 365.0) * 100.0


# ───────────────────────────────
# Core scan
# ───────────────────────────────

def analyze_underlying_iv_crush(client: RESTClient, ticker: str) -> List[str]:
    """
    For one underlying ticker:
      - Pull the full options chain snapshot.
      - Compute implied move for short-dated options with elevated IV.
      - Compare with today's realized move on the stock.
      - Return formatted alert strings for "IV crush candidates".
    """
    alerts: List[str] = []

    # Fetch chain snapshot
    # REST path: /v3/snapshot/options/{underlyingAsset}
    # We'll call it via raw HTTP to avoid guessing the client method name.
    url = f"https://api.polygon.io/v3/snapshot/options/{ticker}"
    params = {"apiKey": POLYGON_KEY}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.exception("IV crush snapshot fetch failed for %s: %s", ticker, e)
        return alerts

    results = data.get("results") or []
    if not results:
        return alerts

    # Underlying info (same for all contracts)
    underlying = results[0].get("underlying_asset") or {}
    u_day = underlying.get("day") or {}
    u_price = underlying.get("last") or underlying.get("close") or 0.0
    u_change_pct = u_day.get("change_percent", 0.0) or 0.0

    if not (IVC_MIN_UNDERLYING_PRICE <= float(u_price) <= IVC_MAX_UNDERLYING_PRICE):
        return alerts

    realized_move_pct = abs(float(u_change_pct)) * 100.0  # day.change_percent is typically decimal (e.g. -0.034)
    ts_str = now_est_str()

    for c in results:
        try:
            details = c.get("details") or {}
            day = c.get("day") or {}
            iv = c.get("implied_volatility") or 0.0
            oi = c.get("open_interest") or 0
            opt_ticker = details.get("ticker") or c.get("ticker")
            expiry = details.get("expiration_date")
            contract_type = details.get("contract_type")
            strike = details.get("strike_price", 0.0)
            opt_volume = day.get("volume", 0)
            opt_last = day.get("close") or day.get("last") or 0.0

            if not opt_ticker or not expiry or contract_type not in ("call", "put"):
                continue

            dte = days_to_expiry(expiry)

            # Filters
            if dte <= 0 or dte > IVC_MAX_DTE:
                continue
            if iv < IVC_MIN_IV:
                continue
            if opt_volume < IVC_MIN_OPTION_VOLUME or oi < IVC_MIN_OPTION_OI:
                continue

            implied_move_pct = estimate_implied_move_pct(iv, dte)
            if implied_move_pct < IVC_MIN_IMPLIED_MOVE_PCT:
                continue

            # "Crush candidate": stock moved much less than what this IV is still implying
            if implied_move_pct <= 0:
                continue

            ratio = realized_move_pct / implied_move_pct

            if ratio <= IVC_MAX_REALIZED_TO_IMPLIED_RATIO:
                direction = "CALL" if contract_type == "call" else "PUT"

                msg = (
                    f"🧊 *IV CRUSH CANDIDATE* — {ticker}\n"
                    f"🕒 {ts_str}\n"
                    f"💰 Price: ${u_price:,.2f} · Δ% {u_change_pct*100:.2f}%\n"
                    f"────────────\n"
                    f"🎯 Contract: `{opt_ticker}` ({direction})\n"
                    f"📅 Exp: {expiry} · DTE: {dte}\n"
                    f"🎯 Strike: ${float(strike):,.2f}\n"
                    f"📊 IV (now): {iv*100:.1f}%\n"
                    f"📦 Vol: {opt_volume:,} · OI: {oi:,}\n"
                    f"📉 Implied move: ~{implied_move_pct:.1f}%\n"
                    f"📉 Realized move today: ~{realized_move_pct:.1f}%\n"
                    f"⚖️ Realized / Implied: {ratio:.2f}x\n"
                    f"────────────\n"
                    f"🔎 This contract is still pricing a much bigger move than the stock actually made.\n"
                    f"    Classic post-event IV-crush setup.\n"
                    f"🔗 Chart: https://www.tradingview.com/chart/?symbol={ticker}"
                )

                alerts.append(msg)

        except Exception as e:
            logger.exception("Error processing contract for %s: %s", ticker, e)
            continue

    return alerts


# ───────────────────────────────
# Public entrypoint
# ───────────────────────────────

def run_iv_crush() -> None:
    """
    Main entrypoint called by your scheduler/orchestrator.
    """
    if not POLYGON_KEY:
        logger.error("POLYGON_KEY is not set; aborting IV Crush bot.")
        return

    client = RESTClient(POLYGON_KEY)

    universe = get_most_active_universe(limit=80)
    logger.info("[iv_crush] Universe size: %d", len(universe))

    total_alerts = 0
    for t in universe:
        alerts = analyze_underlying_iv_crush(client, t)
        for msg in alerts:
            send_telegram(msg)
            total_alerts += 1
            # light pacing to be nice to Telegram
            time.sleep(0.5)

    logger.info("[iv_crush] Done. Alerts sent: %d", total_alerts)


if __name__ == "__main__":
    run_iv_crush()