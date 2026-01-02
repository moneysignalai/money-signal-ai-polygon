"""Gap Flow bot

Detects liquid gap-up and gap-down setups and looks for continuation behavior
(holding VWAP / gap range) intraday. Filters lean on gap magnitude, dollar
volume, and RVOL so only meaningful, liquid gaps surface.
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
_min_gap_pct = float(os.getenv("MIN_PREMARKET_MOVE_PCT", os.getenv("GAP_MIN_GAP_PCT", "3.0")))
_min_dollar_vol = float(
    os.getenv("MIN_PREMARKET_DOLLAR_VOL", os.getenv("GAP_MIN_DOLLAR_VOL", "150000"))
)
_min_rvol = float(os.getenv("GAP_MIN_RVOL", "1.5"))
_min_price = float(os.getenv("GAP_MIN_PRICE", "5"))
_default_max_universe = int(os.getenv("DYNAMIC_MAX_TICKERS", "2000"))
_max_universe = int(
    os.getenv(
        "GAP_FLOW_MAX_UNIVERSE", os.getenv("GAP_SCANNER_MAX_UNIVERSE", str(_default_max_universe))
    )
)
_lookback_days = int(os.getenv("GAP_LOOKBACK_DAYS", "20"))
_gap_hold_threshold = float(os.getenv("GAP_HOLD_THRESHOLD", "0.6"))

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


def _fetch_intraday(sym: str) -> List:
    """Fetch today's minute bars for intraday context."""

    if not _client:
        return []
    today = date.today().isoformat()
    try:
        return list(
            _client.list_aggs(
                sym,
                1,
                "minute",
                today,
                today,
                limit=5000,
                sort="asc",
            )
        )
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"[gap_flow] intraday agg error for {sym}: {exc}")
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
    *,
    symbol: str,
    gap_pct: float,
    day_change_pct: float,
    open_: float,
    high: float,
    low: float,
    last: float,
    rvol: float,
    intraday_volume: float,
    dollar_vol: float,
    holding_gap: bool,
    holding_vwap: bool,
    direction_up: bool,
    ts: datetime,
) -> str:
    """Return a premium gap-flow alert body with continuation context."""

    ts_str = format_est_timestamp(ts)
    header = f"🧠 GAP FLOW — {symbol}\n🕒 {ts_str}\n"

    direction_text = "Gap Up" if direction_up else "Gap Down"
    arrow = "🔼" if direction_up else "🔻"

    rvol_text = f"{rvol:.1f}×" if rvol > 0 else "N/A"
    volume_text = f"{intraday_volume:,.0f}" if intraday_volume > 0 else "N/A"
    dollar_text = f"${dollar_vol:,.0f}" if dollar_vol > 0 else "N/A"
    hold_gap_text = "YES" if holding_gap else "NO"
    hold_vwap_text = "YES" if holding_vwap else "NO"
    direction_ctx = "Bullish continuation gap" if direction_up else "Bearish continuation gap"

    lines = [
        header.rstrip(),
        "",
        "💰 Gap Stats",
        f"• Gap: {gap_pct:+.1f}% vs prior close ({direction_text} {arrow})",
        f"• Day Move: {day_change_pct:+.1f}% vs prior close",
        f"• O {_fmt_price(open_)} · H {_fmt_price(high)} · L {_fmt_price(low)} · Last {_fmt_price(last)}",
        "",
        "📊 Liquidity",
        f"• Volume: {volume_text}",
        f"• RVOL: {rvol_text}",
        f"• Dollar Vol: {dollar_text}",
        "",
        "📈 Continuation Context",
        f"• Holding above VWAP: {hold_vwap_text}",
        f"• Holding >{int(_gap_hold_threshold * 100)}% of gap range: {hold_gap_text}",
        f"• Direction: {direction_ctx}",
        "",
        "🧠 Read",
        "Strong gap-and-go behavior with real volume behind the move.",
        "",
        "🔗 Chart",
        chart_link(symbol),
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
                intraday = _fetch_intraday(sym)
            except Exception as exc:
                print(f"[gap_flow] data error for {sym}: {exc}")
                record_error(BOT_NAME, exc)
                continue

            if len(daily) < 2 or not intraday:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "insufficient_history")
                reason_counts["insufficient_history"] = reason_counts.get(
                    "insufficient_history", 0
                ) + 1
                continue

            today_bar = daily[-1]
            prev_bar = daily[-2]
            daily_open, daily_high, daily_low, daily_close, _ = _extract_ohlcv(today_bar)
            _, _, _, prev_close, _ = _extract_ohlcv(prev_bar)

            intraday_open, intraday_high, intraday_low, intraday_close, intraday_volume = 0.0, 0.0, float("inf"), 0.0, 0.0
            vwap_num = 0.0
            for bar in intraday:
                o, h, l, c, v = _extract_ohlcv(bar)
                if intraday_open == 0.0:
                    intraday_open = o
                intraday_close = c
                intraday_high = max(intraday_high, h)
                intraday_low = min(intraday_low, l)
                intraday_volume += v
                vw = float(getattr(bar, "vw", getattr(bar, "vw", 0.0)) or c)
                vwap_num += vw * v

            if intraday_low == float("inf"):
                intraday_low = 0.0

            total_vol = intraday_volume if intraday_volume > 0 else 0.0
            vwap = (vwap_num / total_vol) if total_vol else 0.0

            if prev_close <= 0 or intraday_open <= 0 or intraday_close <= 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "bad_prices")
                reason_counts["bad_prices"] = reason_counts.get("bad_prices", 0) + 1
                continue

            gap_pct = ((intraday_open - prev_close) / prev_close) * 100
            gap_amount = abs(intraday_open - prev_close)
            direction_up = gap_pct > 0
            day_change_pct = ((intraday_close - prev_close) / prev_close) * 100 if prev_close else 0.0

            recent_volumes = [
                _extract_ohlcv(bar)[4] for bar in daily[-(_lookback_days + 1) : -1]
            ]
            avg_vol = mean(recent_volumes) if recent_volumes else 0.0
            rvol = (intraday_volume / avg_vol) if avg_vol else 0.0
            ref_price = vwap if vwap > 0 else intraday_open
            dollar_vol = intraday_volume * ref_price

            if abs(gap_pct) < _min_gap_pct:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "gap_too_small")
                reason_counts["gap_too_small"] = reason_counts.get("gap_too_small", 0) + 1
                continue

            if intraday_close < _min_price:
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

            eff_rvol_min = max(MIN_RVOL_GLOBAL, _min_rvol, 1.5)
            if rvol < eff_rvol_min:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "rvol_too_low")
                reason_counts["rvol_too_low"] = reason_counts.get("rvol_too_low", 0) + 1
                continue

            if gap_amount <= 0:
                if DEBUG_FLOW_REASONS:
                    debug_filter_reason(BOT_NAME, sym, "gap_amount_zero")
                reason_counts["gap_amount_zero"] = reason_counts.get(
                    "gap_amount_zero", 0
                ) + 1
                continue

            if direction_up:
                hold_pct = (intraday_close - prev_close) / gap_amount
                holding_gap = hold_pct >= _gap_hold_threshold
                holding_vwap = vwap > 0 and intraday_close >= vwap
                if intraday_close <= prev_close or not (holding_gap or holding_vwap):
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "no_up_continuation")
                    reason_counts["no_up_continuation"] = reason_counts.get(
                        "no_up_continuation", 0
                    ) + 1
                    continue
            else:
                hold_pct = (prev_close - intraday_close) / gap_amount
                holding_gap = hold_pct >= _gap_hold_threshold
                holding_vwap = vwap > 0 and intraday_close <= vwap
                if intraday_close >= prev_close or not (holding_gap or holding_vwap):
                    if DEBUG_FLOW_REASONS:
                        debug_filter_reason(BOT_NAME, sym, "no_down_continuation")
                    reason_counts["no_down_continuation"] = reason_counts.get(
                        "no_down_continuation", 0
                    ) + 1
                    continue

            matches += 1
            try:
                alert_text = _format_gap_alert(
                    symbol=sym,
                    gap_pct=gap_pct,
                    day_change_pct=day_change_pct,
                    open_=intraday_open or daily_open,
                    high=intraday_high or daily_high,
                    low=intraday_low or daily_low,
                    last=intraday_close,
                    rvol=rvol,
                    intraday_volume=intraday_volume,
                    dollar_vol=dollar_vol,
                    holding_gap=holding_gap,
                    holding_vwap=holding_vwap,
                    direction_up=direction_up,
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
        day_change_pct=7.1,
        open_=158.49,
        high=184.40,
        low=158.49,
        last=182.64,
        rvol=6.3,
        intraday_volume=3_059_410,
        dollar_vol=484_885_891,
        holding_gap=True,
        holding_vwap=True,
        direction_up=True,
        ts=datetime(2025, 12, 30, 9, 45),
    )
    print(example)


# Backward compatibility alias for any lingering imports
async def run_gap_scanner() -> None:
    await run_gap_flow()
