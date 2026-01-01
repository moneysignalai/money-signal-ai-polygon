"""Gap Flow bot

Detects liquid gap-up and gap-down setups during regular trading hours using
price and volume only. Filters are env-driven and emphasize meaningful gaps
with supportive dollar volume and RVOL so the feed surfaces actionable, liquid
symbols.
"""

import os
import time
from datetime import date, datetime, timedelta
from statistics import mean
from typing import List, Tuple

try:
    from massive import RESTClient
except ImportError:  # pragma: no cover
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

BOT_NAME = "gap_flow"

_allow_outside_rth = (
    os.getenv("GAP_FLOW_ALLOW_OUTSIDE_RTH")
    or os.getenv("GAP_SCANNER_ALLOW_OUTSIDE_RTH", "false")
).lower() == "true"
_min_gap_pct = float(os.getenv("GAP_MIN_GAP_PCT", "3.0"))
_min_dollar_vol = float(os.getenv("GAP_MIN_DOLLAR_VOL", "150000"))
_min_rvol = float(os.getenv("GAP_MIN_RVOL", "1.2"))
_min_price = float(os.getenv("GAP_MIN_PRICE", "5"))
_default_max_universe = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
_max_universe = int(
    os.getenv(
        "GAP_FLOW_MAX_UNIVERSE", os.getenv("GAP_SCANNER_MAX_UNIVERSE", str(_default_max_universe))
    )
)
_lookback_days = int(os.getenv("GAP_LOOKBACK_DAYS", "20"))

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
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"[gap_flow] daily agg error for {sym}: {exc}")
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


def _format_gap_alert(
    symbol: str,
    gap_pct: float,
    rvol: float,
    open_: float,
    high: float,
    low: float,
    close: float,
    intraday_volume: float,
    dollar_vol: float,
    ts: datetime,
) -> str:
    direction_up = gap_pct > 0
    header_emoji = "🚀" if direction_up else "🔻"
    arrow = "🔼" if direction_up else "🔻"
    ts_str = format_est_timestamp(ts)
    header = f"{header_emoji} GAP FLOW — {symbol} ({ts_str})"

    rvol_text = f"{rvol:.1f}x" if rvol > 0 else "N/A"
    volume_text = f"{intraday_volume:,.0f}" if intraday_volume > 0 else "N/A"
    dollar_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"

    lines = [
        header,
        "────────────",
        f"• Direction: {'Gap Up' if direction_up else 'Gap Down'} ({arrow} {gap_pct:+.1f}% vs prior close)",
        f"• 💵 Last: {_fmt_price(close)} (O: {_fmt_price(open_)}, H: {_fmt_price(high)}, L: {_fmt_price(low)})",
        f"• 📊 RVOL: {rvol_text} | Volume: {volume_text}",
        f"• 💰 Dollar Vol: {dollar_text}",
        f"• 📈 Chart: {chart_link(symbol)}",
    ]
    return "\n".join(lines)


async def run_gap_flow() -> None:
    start = time.perf_counter()
    scanned = matches = alerts = 0
    reason_counts: dict[str, int] = {}

    try:
        if not _allow_outside_rth and not in_rth_window_est():
            print("[gap_flow] outside RTH; skipping")
            return record_bot_stats(BOT_NAME, 0, 0, 0, time.perf_counter() - start)

        universe = resolve_universe_for_bot(
            bot_name=BOT_NAME,
            max_universe_env="GAP_FLOW_MAX_UNIVERSE",
            default_max_universe=_max_universe,
        )
        print(f"[gap_flow] universe_size={len(universe)}")
        if not universe:
            record_bot_stats(BOT_NAME, 0, 0, 0, 0.0)
            return

        for sym in universe:
            scanned += 1
            try:
                daily = _fetch_daily(sym, max(_lookback_days, 30))
            except Exception as exc:
                print(f"[gap_flow] data error for {sym}: {exc}")
                record_error(BOT_NAME, exc)
                continue

            if len(daily) < 2:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "insufficient_history")
                reason_counts["insufficient_history"] = reason_counts.get(
                    "insufficient_history", 0
                ) + 1
                continue

            today_bar = daily[-1]
            prev_bar = daily[-2]
            open_, high, low, close, volume = _extract_ohlcv(today_bar)
            _, _, _, prev_close, _ = _extract_ohlcv(prev_bar)

            if prev_close <= 0 or open_ <= 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "bad_prices")
                reason_counts["bad_prices"] = reason_counts.get("bad_prices", 0) + 1
                continue

            gap_pct = ((open_ - prev_close) / prev_close) * 100
            intraday_volume = volume

            recent_volumes = [
                _extract_ohlcv(bar)[4] for bar in daily[-(_lookback_days + 1) : -1]
            ]
            avg_vol = mean(recent_volumes) if recent_volumes else 0.0
            rvol = (intraday_volume / avg_vol) if avg_vol else 0.0
            dollar_vol = intraday_volume * open_

            if abs(gap_pct) < _min_gap_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "gap_too_small")
                reason_counts["gap_too_small"] = reason_counts.get("gap_too_small", 0) + 1
                continue

            if close < _min_price:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "price_too_low")
                reason_counts["price_too_low"] = reason_counts.get("price_too_low", 0) + 1
                continue

            if dollar_vol < max(MIN_VOLUME_GLOBAL, _min_dollar_vol):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "dollar_vol_too_low")
                reason_counts["dollar_vol_too_low"] = reason_counts.get(
                    "dollar_vol_too_low", 0
                ) + 1
                continue

            if rvol < max(MIN_RVOL_GLOBAL, _min_rvol):
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "rvol_too_low")
                reason_counts["rvol_too_low"] = reason_counts.get("rvol_too_low", 0) + 1
                continue

            matches += 1
            try:
                alert_text = _format_gap_alert(
                    symbol=sym,
                    gap_pct=gap_pct,
                    rvol=rvol,
                    open_=open_,
                    high=high,
                    low=low,
                    close=close,
                    intraday_volume=intraday_volume,
                    dollar_vol=dollar_vol,
                    ts=datetime.now(),
                )
                send_alert_text(alert_text)
                alerts += 1
            except Exception as exc:
                print(f"[gap_flow] alert error for {sym}: {exc}")
                continue

        if DEBUG_FLOW_REASONS and alerts == 0 and matches == 0:
            print(f"[gap_flow] No alerts. Filter breakdown: {reason_counts}")
    except Exception as exc:
        print(f"[gap_flow] error: {exc}")
        record_error(BOT_NAME, exc)
    finally:
        runtime = time.perf_counter() - start
        record_bot_stats(BOT_NAME, scanned, matches, alerts, runtime)
        print(
            f"[gap_flow] scan complete: scanned={scanned} matched={matches} "
            f"alerts={alerts} runtime={runtime:.2f}s"
        )


async def run_bot() -> None:  # legacy alias
    await run_gap_flow()


if __name__ == "__main__":  # simple formatter demo
    example = _format_gap_alert(
        symbol="AXSM",
        gap_pct=6.5,
        rvol=6.3,
        open_=158.49,
        high=184.40,
        low=158.49,
        close=182.64,
        intraday_volume=3_059_410,
        dollar_vol=484_885_891,
        ts=datetime(2025, 12, 30, 9, 45),
    )
    print(example)


# Backward compatibility alias for any lingering imports
async def run_gap_scanner() -> None:
    await run_gap_flow()
