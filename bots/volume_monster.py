"""Volume Monster bot

Scans a liquid universe of equities for outsized volume spikes with notable
price moves during regular trading hours.

Signals target classic "tape is screaming" flows: high dollar volume, strong
RVOL, and a meaningful price swing. Alerts are concise and stats are always
recorded for heartbeat visibility.
"""

import os
import time
from datetime import date, datetime, timedelta
from statistics import mean
from typing import List, Tuple

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover - fallback for local dev
    from polygon import RESTClient

from bots.shared import (
    DEBUG_FLOW_REASONS,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    POLYGON_KEY,
    chart_link,
    debug_filter_reason,
    format_est_timestamp,
    in_rth_window_est,
    resolve_universe_for_bot,
    send_alert_text,
)
from bots.status_report import record_bot_stats, record_error

BOT_NAME = "volume_monster"

_allow_outside_rth = os.getenv("VOLUME_MONSTER_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
_min_dollar_vol = float(os.getenv("VOLUME_MONSTER_MIN_DOLLAR_VOL", "150000"))
_min_rvol = float(os.getenv("VOLUME_MONSTER_RVOL", os.getenv("VOLUME_MIN_RVOL", "1.6")))
_min_move_pct = float(os.getenv("VOLUME_MONSTER_MIN_MOVE_PCT", "3.0"))
_max_universe = int(os.getenv("VOLUME_MONSTER_MAX_UNIVERSE", os.getenv("DYNAMIC_MAX_TICKERS", "2000")))
_lookback_days = int(os.getenv("VOLUME_MONSTER_LOOKBACK_DAYS", "20"))

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None


def _fetch_daily(sym: str, days: int) -> List:
    if not _client:
        return []
    start = (date.today() - timedelta(days=days + 5)).isoformat()
    end = date.today().isoformat()
    try:
        return list(
            _client.list_aggs(
                sym,
                1,
                "day",
                start,
                end,
                limit=days + 10,
                sort="asc",
            )
        )
    except Exception as exc:
        print(f"[volume_monster] daily agg error for {sym}: {exc}")
        return []


def _extract_ohlcv(bar: any) -> Tuple[float, float, float, float, float]:
    open_ = float(getattr(bar, "open", getattr(bar, "o", 0.0)) or 0.0)
    high = float(getattr(bar, "high", getattr(bar, "h", 0.0)) or 0.0)
    low = float(getattr(bar, "low", getattr(bar, "l", 0.0)) or 0.0)
    close = float(getattr(bar, "close", getattr(bar, "c", 0.0)) or 0.0)
    volume = float(getattr(bar, "volume", getattr(bar, "v", 0.0)) or 0.0)
    return open_, high, low, close, volume


def _fmt_price(val: float) -> str:
    return f"${val:,.2f}" if val > 0 else "N/A"


def _format_volume_monster_alert(
    symbol: str,
    price: float,
    open_: float,
    high: float,
    low: float,
    rvol: float,
    day_vol: float,
    dollar_vol: float,
    move_pct: float,
    ts: datetime,
) -> str:
    ts_str = format_est_timestamp(ts)
    rvol_text = f"{rvol:.1f}x" if rvol > 0 else "N/A"
    volume_text = f"{day_vol:,.0f}" if day_vol > 0 else "N/A"
    dollar_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"

    lines = [
        f"🚨 VOLUME MONSTER — {symbol} ({ts_str})",
        "────────────",
        f"• 💵 Last: {_fmt_price(price)} (O: {_fmt_price(open_)}, H: {_fmt_price(high)}, L: {_fmt_price(low)})",
        f"• 📊 RVOL: {rvol_text} | Volume: {volume_text} ({rvol_text} avg)",
        f"• 💰 Dollar Vol: {dollar_text}",
        f"• 📈 Chart: {chart_link(symbol, timeframe='D')}",
    ]

    return "\n".join(lines)


def _current_day_stats(sym: str) -> Tuple[float, float, float, float, float, float, float, float]:
    daily = _fetch_daily(sym, max(_lookback_days, 30))
    if len(daily) < 2:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0

    today_bar = daily[-1]
    prev_bar = daily[-2]
    _, _, _, prev_close, _ = _extract_ohlcv(prev_bar)
    open_, high, low, close, vol = _extract_ohlcv(today_bar)

    history = daily[:-1]
    volumes = [
        _extract_ohlcv(b)[4]
        for b in history[-_lookback_days:]
        if _extract_ohlcv(b)[4] > 0
    ]
    avg_vol = mean(volumes) if volumes else 0.0
    rvol = vol / avg_vol if avg_vol > 0 else 0.0
    dollar_vol = close * vol
    change_pct = ((close - prev_close) / prev_close * 100) if prev_close > 0 else 0.0
    return close, dollar_vol, rvol, change_pct, vol, open_, high, low


async def run_volume_monster() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not _allow_outside_rth and not in_rth_window_est():
            print("[volume_monster] outside RTH; skipping")
            return record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)

        universe = resolve_universe_for_bot(
            bot_name="volume_monster",
            max_universe_env="VOLUME_MONSTER_MAX_UNIVERSE",
            default_max_universe=_max_universe,
        )
        print(f"[volume_monster] universe_size={len(universe)}")
        if not universe:
            record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
            return

        for sym in universe:
            scanned += 1
            try:
                price, dollar_vol, rvol, move_pct, day_vol, open_, high, low = _current_day_stats(sym)
            except Exception as exc:
                print(f"[volume_monster] data error for {sym}: {exc}")
                record_error("volume_monster", exc)
                continue

            if price <= 0 or day_vol <= 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("volume_monster", sym, "no_data")
                reason_counts["no_data"] = reason_counts.get("no_data", 0) + 1
                continue

            if dollar_vol < max(_min_dollar_vol, MIN_VOLUME_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("volume_monster", sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] = reason_counts.get(
                    "dollar_vol_too_low", 0
                ) + 1
                continue

            if rvol < max(_min_rvol, MIN_RVOL_GLOBAL):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("volume_monster", sym, "rvol_too_low")
                reason_counts["rvol_too_low"] = reason_counts.get("rvol_too_low", 0) + 1
                continue

            if abs(move_pct) < _min_move_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason("volume_monster", sym, "move_too_small")
                reason_counts["move_too_small"] = reason_counts.get("move_too_small", 0) + 1
                continue

            matches += 1
            try:
                alert_text = _format_volume_monster_alert(
                    symbol=sym,
                    price=price,
                    open_=open_,
                    high=high,
                    low=low,
                    rvol=rvol,
                    day_vol=day_vol,
                    dollar_vol=dollar_vol,
                    move_pct=move_pct,
                    ts=datetime.now(),
                )
                send_alert_text(alert_text)
                alerts += 1
            except Exception as exc:  # pragma: no cover - alert failures shouldn’t crash
                print(f"[volume_monster] alert error for {sym}: {exc}")

        if matches == 0 and DEBUG_FLOW_REASONS:
            print(f"[volume_monster] No alerts. Filter breakdown: {reason_counts}")
    except Exception as exc:
        print(f"[volume_monster] error: {exc}")
        record_error("volume_monster", exc)
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)


if __name__ == "__main__":  # formatter demo
    demo = _format_volume_monster_alert(
        symbol="AXSM",
        price=182.64,
        open_=158.49,
        high=184.40,
        low=158.49,
        rvol=6.3,
        day_vol=3_059_410,
        dollar_vol=558_770_642,
        move_pct=22.8,
        ts=datetime(2025, 12, 30, 14, 21),
    )
    print(demo)
