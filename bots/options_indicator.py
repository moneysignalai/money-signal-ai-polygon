# bots/options_indicator.py
#
# OPTIONS INDICATOR BOT
# Combines:
#   • IV "Rank" (within current chain for that underlying)
#   • RSI(14)
#   • MACD(12, 26, 9)
#   • Bollinger Bands(20, 2)
#   • Volume / RVOL / Open Interest
#
# Goal:
#   Surface underlyings where VOLATILITY + MOMENTUM + PARTICIPATION all line up
#   for either:
#     • HIGH-IV MOMENTUM setups (good for premium-selling / defined-risk short vol)
#     • LOW-IV REVERSAL setups (good for debit spreads, long calls/puts, etc.)
#
# NOTE:
#   The IV "Rank" here is *intra-chain*:
#     Where does the near-ATM IV sit relative to min/max IV across the
#     liquid part of the chain *right now*, not a 12-month historical rank.
#   It still answers: “Is this options board relatively expensive or cheap today?”

import os
import time
from datetime import date, datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any

import math
import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    resolve_universe_for_bot,
    send_alert,
    chart_link,
    is_etf_blacklisted,
    now_est,
)
from bots.status_report import record_bot_stats

eastern = pytz.timezone("US/Eastern")
_client: Optional[RESTClient] = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# ------------ CONFIG ------------

LOOKBACK_DAYS = int(os.getenv("OPTIONS_INDICATOR_LOOKBACK_DAYS", "140"))

DEFAULT_MAX_UNIVERSE = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
MAX_UNIVERSE = int(
    os.getenv("OPTIONS_INDICATOR_MAX_UNIVERSE", str(DEFAULT_MAX_UNIVERSE))
)

# underlying filters
MIN_PRICE = float(os.getenv("OPTIONS_INDICATOR_MIN_PRICE", "10.0"))
MAX_PRICE = float(os.getenv("OPTIONS_INDICATOR_MAX_PRICE", "600.0"))

MIN_DOLLAR_VOL = float(os.getenv("OPTIONS_INDICATOR_MIN_DOLLAR_VOL", "30000000"))  # $30M+
MIN_UNDERLYING_RVOL = float(os.getenv("OPTIONS_INDICATOR_MIN_RVOL", "1.5"))

# IV "rank" thresholds (intra-chain)
HIGH_IV_RANK = float(os.getenv("OPTIONS_INDICATOR_HIGH_IV_RANK", "70.0"))
LOW_IV_RANK = float(os.getenv("OPTIONS_INDICATOR_LOW_IV_RANK", "30.0"))

# RSI thresholds
RSI_OB_OVERBOUGHT = float(os.getenv("OPTIONS_INDICATOR_RSI_OVERBOUGHT", "70.0"))
RSI_OB_OVERSOLD = float(os.getenv("OPTIONS_INDICATOR_RSI_OVERSOLD", "30.0"))

# MACD config
MACD_FAST = int(os.getenv("OPTIONS_INDICATOR_MACD_FAST", "12"))
MACD_SLOW = int(os.getenv("OPTIONS_INDICATOR_MACD_SLOW", "26"))
MACD_SIGNAL = int(os.getenv("OPTIONS_INDICATOR_MACD_SIGNAL", "9"))

# DTE + moneyness window for IV / OI sampling
MIN_DTE = int(os.getenv("OPTIONS_INDICATOR_MIN_DTE", "7"))
MAX_DTE = int(os.getenv("OPTIONS_INDICATOR_MAX_DTE", "60"))
MONEYNESS_PCT = float(os.getenv("OPTIONS_INDICATOR_MONEYNESS_PCT", "0.15"))  # ±15% around spot

BOT_NAME = "options_indicator"


# ------------ TIME WINDOW (RTH) ------------

def _in_rth_window() -> bool:
    """
    Run only during regular trading hours 09:30–16:00 ET.
    """
    now = datetime.now(eastern)
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 16 * 60


# ------------ SMALL HELPERS ------------

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _ema(values: List[float], period: int) -> List[float]:
    if not values or period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out: List[float] = []
    ema_prev = sum(values[:period]) / float(period)
    out.append(ema_prev)
    for v in values[period:]:
        ema_prev = v * k + ema_prev * (1.0 - k)
        out.append(ema_prev)
    return out


def _rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi


def _macd(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (macd_line, signal_line) for last bar.
    """
    if len(values) < max(MACD_FAST, MACD_SLOW, MACD_SIGNAL) + 5:
        return None, None

    ema_fast = _ema(values, MACD_FAST)
    ema_slow = _ema(values, MACD_SLOW)
    # align lengths
    min_len = min(len(ema_fast), len(ema_slow))
    ema_fast = ema_fast[-min_len:]
    ema_slow = ema_slow[-min_len:]
    macd_line_series = [f - s for f, s in zip(ema_fast, ema_slow)]

    signal_series = _ema(macd_line_series, MACD_SIGNAL)
    if not signal_series:
        return None, None

    macd_line = macd_line_series[-1]
    signal_line = signal_series[-1]
    return macd_line, signal_line


def _bollinger(values: List[float], window: int = 20, num_std: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(values) < window:
        return None, None, None
    window_vals = values[-window:]
    mean = sum(window_vals) / float(window)
    var = sum((v - mean) ** 2 for v in window_vals) / float(window)
    std = math.sqrt(var)
    upper = mean + num_std * std
    lower = mean - num_std * std
    return lower, mean, upper


def _universe() -> List[str]:
    # Underlying equities universe; scanned count is number of underlyings evaluated
    # for option indicators, not contracts.
    return resolve_universe_for_bot(
        bot_name="options_indicator",
        bot_env_var="OPTIONS_INDICATOR_TICKER_UNIVERSE",
        max_universe_env="OPTIONS_INDICATOR_MAX_UNIVERSE",
        default_max_universe=DEFAULT_MAX_UNIVERSE,
        apply_dynamic_filters=True,
    )


def _fetch_daily_history(sym: str, trading_day: date, lookback_days: int) -> List[Any]:
    if not _client:
        return []
    try:
        start = (trading_day - timedelta(days=lookback_days)).isoformat()
        end = trading_day.isoformat()
        days = list(
            _client.list_aggs(
                ticker=sym,
                multiplier=1,
                timespan="day",
                from_=start,
                to=end,
                limit=lookback_days,
                sort="asc",
            )
        )
        return days
    except Exception as e:
        print(f"[options_indicator] daily fetch failed for {sym}: {e}")
        return []


# ------------ IV / OI SNAPSHOT (INTRA-CHAIN "RANK") ------------

def _calc_iv_rank_and_oi(sym: str, spot: float) -> Tuple[Optional[float], int, int]:
    """
    Build a quick intra-chain IV “rank” and OI snapshot for a symbol:

      • Filter options:
          - DTE [MIN_DTE, MAX_DTE]
          - strike within ±MONEYNESS_PCT of spot
          - has implied_volatility
      • IV Rank (0–100):
          where atm_iv sits between min_iv & max_iv of this filtered set.
      • Return:
          (iv_rank_0_100 or None, total_oi, max_oi_single_strike)
    """
    from bots.shared import get_option_chain_cached  # local import to avoid cycles

    if spot <= 0:
        return None, 0, 0

    chain = get_option_chain_cached(sym)
    if not chain:
        return None, 0, 0

    opts = chain.get("results") or chain.get("result") or chain.get("options") or []
    if not isinstance(opts, list) or not opts:
        return None, 0, 0

    today = date.today()
    iv_values: List[float] = []
    atm_candidates: List[Tuple[float, float, int]] = []  # (abs_moneyness, iv, oi)
    total_oi = 0
    max_oi = 0

    for opt in opts:
        details = opt.get("details") or {}
        exp_str = details.get("expiration_date")
        if not exp_str:
            continue
        try:
            expiry = date.fromisoformat(exp_str)
        except Exception:
            continue

        dte = (expiry - today).days
        if dte < MIN_DTE or dte > MAX_DTE:
            continue

        strike = _safe_float(details.get("strike_price"), default=0.0)
        if strike <= 0:
            continue

        # moneyness filter
        rel = abs(strike - spot) / spot
        if rel > MONEYNESS_PCT:
            continue

        iv = opt.get("implied_volatility") or (opt.get("day") or {}).get("implied_volatility")
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if iv <= 0:
            continue

        oi = int(details.get("open_interest") or opt.get("open_interest") or 0)
        total_oi += oi
        if oi > max_oi:
            max_oi = oi

        iv_values.append(iv)
        atm_candidates.append((rel, iv, oi))

    if not iv_values:
        return None, total_oi, max_oi

    iv_min = min(iv_values)
    iv_max = max(iv_values)
    if iv_max <= iv_min:
        # flat-ish board
        return 50.0, total_oi, max_oi

    # choose "ATM" as the contract closest to spot by moneyness
    atm_candidates.sort(key=lambda x: x[0])
    _, atm_iv, _ = atm_candidates[0]

    iv_rank = (atm_iv - iv_min) / (iv_max - iv_min) * 100.0
    iv_rank = max(0.0, min(100.0, iv_rank))
    return iv_rank, total_oi, max_oi


# ------------ CORE LOGIC PER SYMBOL ------------

def _evaluate_symbol(sym: str, days: List[Any]) -> Optional[Dict[str, Any]]:
    """
    Returns a dict with signal info if this symbol qualifies, else None.

    We classify into:
      • HIGH_IV_MOMENTUM
      • LOW_IV_REVERSAL

    Both require:
      • Sane price
      • Healthy dollar volume & RVOL
      • Valid RSI, MACD, Bollinger, IV Rank snapshot
    """
    if len(days) < 60:
        return None

    today_bar = days[-1]
    prev_bar = days[-2]

    try:
        close = float(today_bar.close)
        prev_close = float(prev_bar.close)
        vol = float(today_bar.volume or 0.0)
    except Exception:
        return None

    if close < MIN_PRICE or close > MAX_PRICE:
        return None

    dollar_vol = close * vol
    if dollar_vol < max(MIN_DOLLAR_VOL, MIN_VOLUME_GLOBAL * close):
        return None

    # RVOL vs last ~20 days
    vols = [float(d.volume or 0.0) for d in days[-21:-1]]
    avg_vol = sum(vols) / max(len(vols), 1)
    if avg_vol <= 0:
        return None
    rvol = vol / avg_vol
    if rvol < max(MIN_UNDERLYING_RVOL, MIN_RVOL_GLOBAL):
        return None

    closes = [float(d.close) for d in days]

    # RSI(14)
    rsi = _rsi(closes, period=14)
    if rsi is None:
        return None

    # MACD
    macd_line, signal_line = _macd(closes)
    if macd_line is None or signal_line is None:
        return None

    # Bollinger
    bb_lower, bb_mid, bb_upper = _bollinger(closes, window=20, num_std=2.0)
    if bb_lower is None or bb_mid is None or bb_upper is None:
        return None

    # IV rank + OI snapshot
    iv_rank, total_oi, max_oi = _calc_iv_rank_and_oi(sym, close)
    if iv_rank is None:
        return None

    # Basic classification logic

    # Price change
    move_pct = (close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0

    # Where is price vs Bollinger?
    bb_pos = None
    try:
        bb_pos = (close - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else None
    except Exception:
        bb_pos = None

    # Determine regime
    regime = None
    bias_text = ""

    bullish_macd = macd_line > signal_line and macd_line > 0
    bearish_macd = macd_line < signal_line and macd_line < 0

    near_lower_band = bb_pos is not None and bb_pos < 0.25
    near_upper_band = bb_pos is not None and bb_pos > 0.75

    # HIGH IV, trend/strength likely up → good for premium-selling / defined risk short vol
    if (
        iv_rank >= HIGH_IV_RANK
        and rsi > 50.0
        and bullish_macd
        and not near_lower_band
    ):
        regime = "HIGH_IV_MOMENTUM"
        bias_text = (
            "Elevated IV with bullish momentum — candidates for premium-selling or "
            "defined-risk short-vol structures (e.g., call credit spreads, iron condors "
            "slightly OTM), depending on your playbook."
        )

    # LOW IV with oversold / potential reversal → good for long premium / debit spreads
    elif (
        iv_rank <= LOW_IV_RANK
        and rsi < 50.0
        and (near_lower_band or rsi <= RSI_OB_OVERSOLD + 5)
        and not bearish_macd  # avoid obvious downtrends
    ):
        regime = "LOW_IV_REVERSAL"
        bias_text = (
            "Relatively cheap IV with potential reversal setup — candidates for long "
            "premium (calls/puts) or debit spreads where defined risk and convexity "
            "matter more than immediate IV expansion."
        )

    if not regime:
        return None

    return {
        "sym": sym,
        "close": close,
        "rvol": rvol,
        "rsi": rsi,
        "macd": macd_line,
        "signal": signal_line,
        "bb_lower": bb_lower,
        "bb_mid": bb_mid,
        "bb_upper": bb_upper,
        "bb_pos": bb_pos,
        "iv_rank": iv_rank,
        "total_oi": total_oi,
        "max_oi": max_oi,
        "dollar_vol": dollar_vol,
        "move_pct": move_pct,
        "regime": regime,
        "bias_text": bias_text,
    }


# ------------ MAIN BOT ------------

async def run_options_indicator() -> None:
    """
    Combined options/underlying indicator bot.

    For each symbol in a dynamic universe:
      • Pull daily candles, compute RSI / MACD / Bollinger / RVOL.
      • Snapshot the options chain and derive:
          – IV “Rank” (intra-chain)
          – Total OI + Max OI on liquid near-ATM strikes.
      • If conditions line up:
          – HIGH_IV_MOMENTUM
          – LOW_IV_REVERSAL
        …fire a single, clean alert describing the regime and indicators.
    """
    if not POLYGON_KEY or not _client:
        print("[options_indicator] POLYGON_KEY or client missing; skipping.")
        return

    if not _in_rth_window():
        print("[options_indicator] Outside RTH window; skipping.")
        return

    universe = _universe()
    if not universe:
        print("[options_indicator] empty universe; skipping.")
        return

    trading_day = date.today()
    start_ts = time.time()
    alerts_sent = 0
    matches: List[str] = []

    print(f"[options_indicator] scanning {len(universe)} symbols @ {now_est()}")

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        days = _fetch_daily_history(sym, trading_day, LOOKBACK_DAYS)
        if not days:
            continue

        info = _evaluate_symbol(sym, days)
        if not info:
            continue

        matches.append(sym)
        alerts_sent += 1

        # Build alert
        regime = info["regime"]
        close = info["close"]
        rvol = info["rvol"]
        rsi = info["rsi"]
        macd_line = info["macd"]
        signal_line = info["signal"]
        bb_lower = info["bb_lower"]
        bb_mid = info["bb_mid"]
        bb_upper = info["bb_upper"]
        iv_rank = info["iv_rank"]
        total_oi = info["total_oi"]
        max_oi = info["max_oi"]
        dollar_vol = info["dollar_vol"]
        move_pct = info["move_pct"]
        bias_text = info["bias_text"]

        timestamp = now_est()
        if not isinstance(timestamp, str):
            timestamp = timestamp.strftime("%I:%M %p EST · %b %d").lstrip("0")

        if regime == "HIGH_IV_MOMENTUM":
            header_emoji = "📈"
            header_text = "HIGH-IV MOMENTUM"
        else:
            header_emoji = "📉"
            header_text = "LOW-IV REVERSAL"

        extra_lines = [
            f"{header_emoji} OPTIONS INDICATOR — {sym}",
            f"🕒 {timestamp}",
            f"💰 Underlying: ${close:.2f} · RVOL {rvol:.1f}x",
            "────────────",
            f"🎯 Regime: {header_text}",
            f"📊 IV Rank (intra-chain): {iv_rank:.0f}",
            f"📉 RSI(14): {rsi:.1f}",
            f"📈 MACD: {macd_line:.3f} vs Signal {signal_line:.3f}",
            f"📎 Bollinger 20/2: Lower {bb_lower:.2f} · Mid {bb_mid:.2f} · Upper {bb_upper:.2f}",
            f"💵 Dollar Volume (today): ≈ ${dollar_vol:,.0f}",
            f"📦 Options OI: total {total_oi:,} · max strike {max_oi:,}",
            f"📊 Day Move: {move_pct:.1f}%",
            "",
            f"🧠 Bias: {bias_text}",
            f"🔗 Chart: {chart_link(sym)}",
        ]

        extra_text = "\n".join(extra_lines)

        # rvol is the underlying RVOL; we surface it in status bot as usual
        send_alert(BOT_NAME, sym, close, rvol, extra=extra_text)

    run_seconds = time.time() - start_ts
    try:
        record_bot_stats(
            BOT_NAME,
            scanned=len(universe),
            matched=len(matches),
            alerts=alerts_sent,
            runtime=run_seconds,
        )
    except Exception as e:
        print(f"[options_indicator] record_bot_stats error: {e}")

    print(
        f"[options_indicator] scan complete: "
        f"scanned={len(universe)} matched={len(matches)} alerts={alerts_sent} "
        f"runtime={run_seconds:.2f}s"
    )