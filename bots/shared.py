# bots/shared.py — core config + Telegram helpers
import os
import requests
from datetime import datetime
import pytz

# Time helpers
eastern = pytz.timezone("US/Eastern")
def now_est() -> str:
    return datetime.now(eastern).strftime("%I:%M %p EST · %b %d")


# Global filters (you can tweak these later)
MIN_RVOL_GLOBAL   = 1.5
MIN_VOLUME_GLOBAL = 300_000
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65

# Env vars
POLYGON_KEY = os.getenv("POLYGON_KEY", "")

# One main chat for all alerts
TELEGRAM_CHAT_ALL = os.getenv("TELEGRAM_CHAT_ALL", "")

# Per-strategy bots (you can reuse the same token for all if you want)
TELEGRAM_TOKEN_DEAL    = os.getenv("TELEGRAM_TOKEN_DEAL", "")
TELEGRAM_TOKEN_EARN    = os.getenv("TELEGRAM_TOKEN_EARN", "")
TELEGRAM_TOKEN_GAP     = os.getenv("TELEGRAM_TOKEN_GAP", "")
TELEGRAM_TOKEN_ORB     = os.getenv("TELEGRAM_TOKEN_ORB", "")
TELEGRAM_TOKEN_SQUEEZE = os.getenv("TELEGRAM_TOKEN_SQUEEZE", "")
TELEGRAM_TOKEN_UNUSUAL = os.getenv("TELEGRAM_TOKEN_UNUSUAL", "")
TELEGRAM_TOKEN_FLOW    = os.getenv("TELEGRAM_TOKEN_FLOW", "")
TELEGRAM_TOKEN_STATUS  = os.getenv("TELEGRAM_TOKEN_STATUS", "")


def _pick_token(bot_name: str) -> str:
    """Map logical bot name → Telegram bot token."""
    name = bot_name.lower()
    if name in ("cheap", "deal"):
        return TELEGRAM_TOKEN_DEAL or TELEGRAM_TOKEN_FLOW
    if name == "earnings":
        return TELEGRAM_TOKEN_EARN or TELEGRAM_TOKEN_FLOW
    if name == "gap":
        return TELEGRAM_TOKEN_GAP or TELEGRAM_TOKEN_FLOW
    if name == "orb":
        return TELEGRAM_TOKEN_ORB or TELEGRAM_TOKEN_FLOW
    if name in ("squeeze", "short_squeeze"):
        return TELEGRAM_TOKEN_SQUEEZE or TELEGRAM_TOKEN_FLOW
    if name == "unusual":
        return TELEGRAM_TOKEN_UNUSUAL or TELEGRAM_TOKEN_FLOW
    if name in ("volume", "top_volume", "premarket"):
        return TELEGRAM_TOKEN_FLOW or TELEGRAM_TOKEN_DEAL
    if name in ("momentum", "momentum_reversal"):
        return TELEGRAM_TOKEN_FLOW or TELEGRAM_TOKEN_DEAL
    # Fallback to status bot
    return TELEGRAM_TOKEN_STATUS or TELEGRAM_TOKEN_FLOW or TELEGRAM_TOKEN_DEAL


def send_alert(
    bot_name: str,
    symbol: str,
    last_price: float,
    rvol: float,
    extra: str = "",
) -> None:
    """
    Core Telegram send function used by all bots.

    bot_name → picks which token to use.
    Everything goes to TELEGRAM_CHAT_ALL.
    """
    if not TELEGRAM_CHAT_ALL:
        print("ALERT SKIPPED: TELEGRAM_CHAT_ALL not set")
        return

    token = _pick_token(bot_name)
    if not token:
        print(f"ALERT SKIPPED: no Telegram token configured for {bot_name}")
        return

    title = bot_name.upper().replace("_", " ")
    msg = f"*{title}* — {symbol}\nPrice: ${last_price:.2f} · RVOL {rvol:.1f}x"

    if extra:
        msg += f"\n\n{extra}"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ALL, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        print(f"ALERT SENT [{bot_name}] {symbol}")
    except Exception as e:
        print(f"ALERT FAILED [{bot_name}] {symbol}: {e}")


def send_status_message(text: str) -> None:
    """Optional status helper if you want to send custom messages."""
    if not TELEGRAM_TOKEN_STATUS or not TELEGRAM_CHAT_ALL:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN_STATUS}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ALL, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def start_polygon_websocket():
    # Placeholder – you can wire real websockets later if you want.
    print("Polygon/WebSocket placeholder — using REST scanners only for now.")