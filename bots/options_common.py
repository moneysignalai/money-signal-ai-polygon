"""Shared helpers for options flow bots.

Provides utilities to resolve option contract details from Massive/Polygon
snapshots so the individual option flow bots can focus on their filters.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bots.shared import (
    chart_link,
    debug_filter_reason,
    eastern,
    DEBUG_FLOW_REASONS,
    get_last_option_trades_cached,
    get_option_chain_cached,
    get_last_trade_cached,
    send_alert_text,
    today_est_date,
)


OPTION_MULTIPLIER = 100


def _safe_float(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def _safe_int(val: Any) -> Optional[int]:
    try:
        if val is None:
            return None
        return int(val)
    except Exception:
        return None


def _contract_type(contract: str) -> Optional[str]:
    if not contract:
        return None
    base = contract.replace("O:", "")
    if "C" in base and "P" not in base:
        return "CALL"
    if "P" in base and "C" not in base:
        return "PUT"
    return None


def _parse_occ(contract: str) -> Dict[str, Optional[Any]]:
    """Parse an OCC formatted contract code into components."""

    if not contract:
        return {"underlying": None, "expiry": None, "cp": None, "strike": None}

    base = contract.replace("O:", "")
    if len(base) < 15:
        return {"underlying": None, "expiry": None, "cp": None, "strike": None}

    underlying = base[:-15]
    date_part = base[-15:-9]
    cp_letter = base[-9]
    strike_part = base[-8:]

    expiry: Optional[str] = None
    try:
        dt_exp = datetime.strptime(date_part, "%y%m%d").date()
        expiry = dt_exp.strftime("%Y-%m-%d")
    except Exception:
        expiry = None

    strike: Optional[float] = None
    try:
        strike = int(strike_part) / 1000.0
    except Exception:
        strike = None

    cp = "CALL" if cp_letter.upper() == "C" else "PUT" if cp_letter.upper() == "P" else None

    return {"underlying": underlying, "expiry": expiry, "cp": cp, "strike": strike}


def _parse_option_details(opt: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    details = opt.get("details") or {}
    alt = opt.get("option") or {}
    contract = (
        details.get("ticker")
        or details.get("symbol")
        or opt.get("ticker")
        or opt.get("symbol")
        or opt.get("option_symbol")
        or alt.get("symbol")
    )
    expiry = (
        details.get("expiration_date")
        or details.get("expiry")
        or details.get("expiration")
        or details.get("exp_date")
        or alt.get("expiration_date")
        or opt.get("expiration_date")
        or opt.get("exp_date")
    )

    dte: Optional[int] = None
    if expiry:
        try:
            fmt = "%Y-%m-%d" if "-" in expiry else "%Y%m%d"
            dt_exp = datetime.strptime(expiry, fmt).date()
            today = datetime.now().date()
            dte = (dt_exp - today).days
        except Exception:
            dte = None
    return expiry, contract, dte


@dataclass
class OptionContract:
    symbol: str
    contract: str
    cp: Optional[str]
    strike: Optional[float]
    expiry: Optional[str]
    dte: Optional[int]
    premium: Optional[float]
    size: Optional[int]
    notional: Optional[float]
    volume: Optional[int]
    open_interest: Optional[int]
    iv: Optional[float]
    underlying_price: Optional[float]
    underlying_open: Optional[float] = None
    underlying_prev_close: Optional[float] = None
    underlying_rvol: Optional[float] = None
    underlying_volume: Optional[int] = None
    price_size_reason: Optional[str] = None


class FlowReasonTracker:
    """Accumulates filter reasons and optional examples for debug mode."""

    def __init__(self, bot_name: str, *, max_examples: int = 3):
        self.bot_name = bot_name
        self.max_examples = max_examples
        self.counts: Dict[str, int] = {}
        self.examples: Dict[str, list[str]] = {}

    def record(self, symbol: str, reason: str) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        if DEBUG_FLOW_REASONS and self.counts[reason] <= self.max_examples:
            self.examples.setdefault(reason, []).append(symbol)
            debug_filter_reason(self.bot_name, symbol, reason)

    def log_summary(self) -> None:
        if not DEBUG_FLOW_REASONS or not self.counts:
            return
        summary = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        print(f"[{self.bot_name}] filter summary: {summary}")
        for reason, samples in sorted(self.examples.items()):
            if samples:
                joined = ", ".join(samples[: self.max_examples])
                print(f"[{self.bot_name}] examples {reason}: {joined}")


def _extract_underlying_fields(chain: Dict[str, Any]) -> Dict[str, Optional[Any]]:
    underlying = chain.get("underlying") or {}
    last = underlying.get("last") or {}
    day = underlying.get("day") or {}
    prev_day = underlying.get("prev_day") or {}

    price = _safe_float(
        last.get("price")
        or underlying.get("last_price")
        or underlying.get("price")
        or underlying.get("close")
    )
    if price is not None and price <= 0:
        price = None

    if price is None:
        # Fallback to open/prev close if last is missing to avoid $0.00 underlyings
        price = _safe_float(underlying.get("close") or underlying.get("previous_close"))

    open_price = _safe_float(day.get("open") or day.get("o") or underlying.get("open"))
    prev_close = _safe_float(
        underlying.get("prev_close")
        or underlying.get("previous_close")
        or prev_day.get("close")
        or prev_day.get("c")
        or day.get("prev_close")
    )
    volume = _safe_int(day.get("volume") or day.get("v") or underlying.get("volume"))
    rvol = _safe_float(underlying.get("rvol") or day.get("rvol"))

    return {
        "price": price,
        "open": open_price,
        "prev_close": prev_close,
        "rvol": rvol,
        "volume": volume,
    }


def _option_iv(opt: Dict[str, Any]) -> Optional[float]:
    return _safe_float(
        opt.get("implied_volatility")
        or opt.get("iv")
        or (opt.get("greeks") or {}).get("iv")
    )


def _ts_to_est(ts_raw: Any) -> Optional[datetime]:
    """Convert trade timestamps (ns/us/ms/s) to Eastern datetime."""

    if ts_raw is None:
        return None
    try:
        ts_int = int(ts_raw)
    except Exception:
        return None

    if ts_int > 1_000_000_000_000_000_000:
        ts_seconds = ts_int / 1_000_000_000
    elif ts_int > 1_000_000_000_000:
        ts_seconds = ts_int / 1_000_000
    elif ts_int > 1_000_000_000:
        ts_seconds = ts_int / 1_000
    else:
        ts_seconds = float(ts_int)

    try:
        dt = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
        return dt.astimezone(eastern)
    except Exception:
        return None


def _is_trade_today(ts_raw: Any) -> bool:
    dt = _ts_to_est(ts_raw)
    return bool(dt and dt.date() == today_est_date())


def iter_option_contracts(
    symbol: str, *, ttl_seconds: int = 60, reason_tracker: Optional[FlowReasonTracker] = None
) -> List[OptionContract]:
    """Return parsed option contracts for an underlying symbol.

    Each contract includes price/size/notional when available. Errors are
    swallowed so callers can safely iterate without crashing.
    """

    chain = get_option_chain_cached(symbol, ttl_seconds=ttl_seconds) or {}
    options = chain.get("results") or chain.get("options") or []
    underlying_fields = _extract_underlying_fields(chain)
    underlying_price = underlying_fields.get("price")

    contracts: List[OptionContract] = []
    for opt in options:
        expiry, contract, dte = _parse_option_details(opt)
        if not contract:
            if reason_tracker:
                reason_tracker.record(symbol, "missing_contract_symbol")
            else:
                debug_filter_reason("options_common", symbol, "missing_contract_symbol")
            continue

        occ_parts = _parse_occ(contract)

        cp = (
            opt.get("type")
            or opt.get("contract_type")
            or _contract_type(contract)
            or occ_parts.get("cp")
        )
        strike = _safe_float(
            (opt.get("details") or {}).get("strike_price")
            or opt.get("strike_price")
            or opt.get("strike")
            or occ_parts.get("strike")
        )
        if not expiry:
            expiry = occ_parts.get("expiry")
        if dte is None and occ_parts.get("expiry"):
            try:
                dt_exp = datetime.strptime(occ_parts["expiry"], "%Y-%m-%d").date()
                dte = (dt_exp - datetime.now().date()).days
            except Exception:
                dte = None

        last_trade_obj = opt.get("last_trade") or opt.get("lastTrade") or opt.get("last") or {}
        trade_ts = (
            last_trade_obj.get("sip_timestamp")
            or last_trade_obj.get("participant_timestamp")
            or last_trade_obj.get("trf_timestamp")
            or last_trade_obj.get("t")
        )
        premium = _safe_float(
            last_trade_obj.get("p")
            or last_trade_obj.get("price")
            or last_trade_obj.get("mid")
            or opt.get("last_price")
            or opt.get("price")
        )
        size = _safe_int(last_trade_obj.get("s") or last_trade_obj.get("size") or opt.get("size"))

        last_trade_present = bool(last_trade_obj)
        last_trade_stale = bool(trade_ts and not _is_trade_today(trade_ts))

        if trade_ts and not _is_trade_today(trade_ts):
            # Ignore stale trades from prior sessions
            premium = None
            size = None

        if (premium is None or size is None) and contract:
            # Try last trade fallback
            try:
                lt = get_last_option_trades_cached(contract)
                trade = lt.get("results") if isinstance(lt, dict) else None
                if trade:
                    last_trade_present = True
                    ts_val = (
                        trade.get("sip_timestamp")
                        or trade.get("participant_timestamp")
                        or trade.get("trf_timestamp")
                        or trade.get("t")
                    )
                    if _is_trade_today(ts_val):
                        premium = premium if premium is not None else _safe_float(trade.get("p") or trade.get("price"))
                        size = size if size is not None else _safe_int(trade.get("s") or trade.get("size"))
                        last_trade_stale = False
                    else:
                        last_trade_stale = True
            except Exception:
                pass

        bid = _safe_float((opt.get("last_quote") or {}).get("bid") or opt.get("bid"))
        ask = _safe_float((opt.get("last_quote") or {}).get("ask") or opt.get("ask"))
        if premium is None and bid is not None and ask is not None:
            premium = (bid + ask) / 2.0

        volume = _safe_int(opt.get("volume") or opt.get("v") or size)
        open_interest = _safe_int(
            opt.get("open_interest")
            or opt.get("oi")
            or (opt.get("details") or {}).get("open_interest")
        )
        notional = None
        if premium is not None and size is not None:
            notional = premium * size * OPTION_MULTIPLIER

        price_size_reason: Optional[str] = None
        if premium is None or size is None:
            if not last_trade_present:
                price_size_reason = "missing_last_trade"
            elif last_trade_stale:
                price_size_reason = "stale_last_trade"
            elif premium is None and (bid is None or ask is None):
                price_size_reason = "missing_quote"
            elif premium is None and size is not None:
                price_size_reason = "missing_premium"
            elif size is None and premium is not None:
                price_size_reason = "missing_size"
            if price_size_reason is None:
                price_size_reason = "missing_price_size"

        local_underlying_price = underlying_price
        if local_underlying_price is None:
            underlying_obj = opt.get("underlying") or {}
            local_underlying_price = _safe_float(
                (underlying_obj.get("last") or {}).get("price")
                or underlying_obj.get("price")
                or opt.get("underlying_price")
                or opt.get("underlying_last")
            )
            if local_underlying_price is not None and local_underlying_price <= 0:
                local_underlying_price = None
            if local_underlying_price is None:
                # As a last resort, pull a cached last trade to avoid $0.00 underlyings
                try:
                    last_price, _ = get_last_trade_cached(symbol)
                    if last_price is not None and last_price > 0:
                        local_underlying_price = last_price
                except Exception:
                    pass

        local_underlying_rvol = underlying_fields.get("rvol")
        if local_underlying_rvol is None:
            underlying_obj = opt.get("underlying") or {}
            local_underlying_rvol = _safe_float(underlying_obj.get("rvol"))

        contracts.append(
            OptionContract(
                symbol=symbol,
                contract=contract,
                cp=cp,
                strike=strike,
                expiry=expiry,
                dte=dte,
                premium=premium,
                size=size,
                notional=notional,
                volume=volume,
                open_interest=open_interest,
                iv=_option_iv(opt),
                underlying_price=local_underlying_price,
                underlying_open=underlying_fields.get("open"),
                underlying_prev_close=underlying_fields.get("prev_close"),
                underlying_rvol=local_underlying_rvol,
                underlying_volume=underlying_fields.get("volume"),
                price_size_reason=price_size_reason,
            )
        )
    return contracts


def options_flow_allow_outside_rth() -> bool:
    return os.getenv("OPTIONS_FLOW_ALLOW_OUTSIDE_RTH", "false").lower() == "true"


def _format_strike(strike: Optional[float]) -> str:
    if strike is None:
        return "?"
    text = f"{strike:.2f}".rstrip("0").rstrip(".")
    return text


def _format_expiry(expiry: Optional[str]) -> str:
    if not expiry:
        return "n/a"
    try:
        fmt = "%Y-%m-%d" if "-" in expiry else "%Y%m%d"
        dt_exp = datetime.strptime(expiry, fmt)
        return dt_exp.strftime("%m-%d-%Y")
    except Exception:
        return expiry


def format_option_contract_display(contract: OptionContract) -> str:
    parsed = _parse_occ(contract.contract)
    expiry = contract.expiry or parsed.get("expiry")
    strike_val = contract.strike if contract.strike is not None else parsed.get("strike")
    cp_val = contract.cp or parsed.get("cp")
    cp_letter = "C" if cp_val and cp_val.upper().startswith("C") else "P" if cp_val and cp_val.upper().startswith("P") else "?"
    ticker = contract.symbol or parsed.get("underlying") or "?"
    return f"{ticker.upper()} {_format_strike(strike_val)}{cp_letter} {_format_expiry(expiry)}"


def format_contract_brief_with_size(contract: OptionContract) -> str:
    """Return a compact contract string with size and strike details."""

    parsed = _parse_occ(contract.contract)
    expiry = contract.expiry or parsed.get("expiry")
    strike_val = contract.strike if contract.strike is not None else parsed.get("strike")
    cp_val = contract.cp or parsed.get("cp")
    cp_letter = "C" if cp_val and cp_val.upper().startswith("C") else "P" if cp_val and cp_val.upper().startswith("P") else "?"
    ticker = contract.symbol or parsed.get("underlying") or "?"
    size_text = f"{contract.size}x " if contract.size is not None else ""
    strike_fmt = _format_strike(strike_val)
    strike_currency = _format_currency(_safe_float(strike_val), decimals=2)
    return f"{size_text}{_format_expiry(expiry)} {strike_fmt}{cp_letter} (Strike {strike_currency})"


def _format_currency(value: Optional[float], *, decimals: int = 2) -> str:
    if value is None or value <= 0:
        return "N/A"
    return f"${value:,.{decimals}f}"


def _underlying_change_pct(contract: OptionContract) -> Optional[float]:
    """Estimate the underlying's day move when reference prices are present."""

    last = contract.underlying_price
    ref = contract.underlying_open or contract.underlying_prev_close
    if last is None or ref is None or ref <= 0:
        return None
    try:
        return ((last - ref) / ref) * 100.0
    except Exception:
        return None


def format_iv_crush_alert(
    *,
    contract: OptionContract,
    prev_iv: Optional[float],
    iv_drop_pct: Optional[float],
    min_drop_pct: float,
    volume_threshold: Optional[int] = None,
    chart_symbol: Optional[str] = None,
    context_line: Optional[str] = None,
    risk_line: Optional[str] = None,
) -> str:
    """Return a rich IV crush alert with parsed contract and IV deltas."""

    now = datetime.now(eastern)
    timestamp = now.strftime("%m-%d-%Y · %I:%M %p EST")
    symbol = (chart_symbol or contract.symbol or "?").upper()

    change_pct = _underlying_change_pct(contract)
    change_text = f" ({change_pct:+.1f}% today)" if change_pct is not None else ""
    rvol_text = (
        f" · RVOL {contract.underlying_rvol:.1f}×" if contract.underlying_rvol is not None else ""
    )
    underlying_line = (
        f"💰 Underlying: {_format_currency(contract.underlying_price)}{change_text}{rvol_text}"
    )

    contract_brief = format_contract_brief_with_size(contract)
    premium_text = _format_currency(contract.premium)
    notional_text = _format_currency(contract.notional, decimals=0)
    volume_text = f"{contract.volume:,}" if contract.volume is not None else "n/a"
    oi_text = f"{contract.open_interest:,}" if contract.open_interest is not None else "n/a"

    iv_before = f"{prev_iv:.1f}%" if prev_iv is not None else "n/a"
    iv_now = f"{contract.iv:.1f}%" if contract.iv is not None else "n/a"
    drop_text = f"{iv_drop_pct:.1f}%" if iv_drop_pct is not None else "n/a"
    drop_note = f"(meets IVCRUSH_MIN_IV_DROP_PCT={min_drop_pct:.0f}%)" if iv_drop_pct is not None else ""

    vol_note = ""
    if volume_threshold is not None and contract.volume is not None:
        vol_note = (
            f" (meets IVCRUSH_MIN_VOL)"
            if contract.volume >= volume_threshold
            else ""
        )

    context = context_line or "Post-event IV collapse with price stabilizing"
    risk = risk_line or "Elevated realized move already happened; options now pricing less future volatility."

    header = f"🔥 IV CRUSH — {symbol}"
    time_line = f"🕒 {timestamp}"
    separator = "────────────"
    contract_line = f"🎯 Contract: {contract_brief}"
    money_line = f"💸 Premium per contract: {premium_text} · Total Notional: {notional_text}"
    iv_block = (
        "📉 IV Crush Details:\n"
        f"• IV before: {iv_before} → IV now: {iv_now}\n"
        f"• IV drop: {drop_text} {drop_note}\n"
        f"• Option volume: {volume_text}{vol_note}"
    )
    context_fmt = f"🧠 Context: {context}"
    risk_fmt = f"⚖️ Risk View: {risk}"
    chart_line = f"🔗 Chart: {chart_link(symbol)}"

    return "\n".join(
        [
            header,
            time_line,
            underlying_line,
            separator,
            contract_line,
            money_line,
            iv_block,
            context_fmt,
            risk_fmt,
            chart_line,
        ]
    )


def format_whale_option_alert(
    *,
    contract: OptionContract,
    flow_tags: Optional[list[str]] = None,
    context_line: Optional[str] = None,
    bias_line: Optional[str] = None,
    chart_symbol: Optional[str] = None,
) -> str:
    """Premium whale-flow alert with parsed contract + underlying context."""

    now = datetime.now(eastern)
    timestamp = now.strftime("%m-%d-%Y · %I:%M %p EST")
    symbol = (chart_symbol or contract.symbol or "?").upper()

    change_pct = _underlying_change_pct(contract)
    change_text = f" ({change_pct:+.1f}% today)" if change_pct is not None else ""
    rvol_text = (
        f" · RVOL {contract.underlying_rvol:.1f}×" if contract.underlying_rvol is not None else ""
    )
    underlying_line = f"💰 Underlying: {_format_currency(contract.underlying_price)}{change_text}{rvol_text}"

    contract_brief = format_contract_brief_with_size(contract)
    dte_text = f"{contract.dte} DTE" if contract.dte is not None else "n/a"
    premium_text = _format_currency(contract.premium)
    notional_text = _format_currency(contract.notional, decimals=0)
    flow_tag_text = " · ".join(flow_tags) if flow_tags else "n/a"

    oi = contract.open_interest or 0
    vol = contract.volume or 0
    ratio_text = "n/a"
    if oi > 0 and vol > 0:
        ratio_text = f"{vol/oi:.1f}× OI"
    elif vol > 0:
        ratio_text = "volume present, no OI data"

    context = context_line or f"Option volume {vol} vs OI {oi} ({ratio_text})"
    bias = bias_line or "Aggressive bullish whale flow"

    header = f"🐳 WHALE FLOW — {symbol}"
    time_line = f"🕒 {timestamp}"
    separator = "────────────"
    order_line = f"📦 Order: {contract_brief} (⏳ {dte_text})"
    money_line = f"💵 Premium per contract: {premium_text} · Total Notional: {notional_text}"
    tags_line = f"📊 Flow tags: {flow_tag_text}"
    context_line_fmt = f"⚖️ Context: {context}"
    bias_line_fmt = f"🧠 Bias: {bias}"
    chart_line = f"🔗 Chart: {chart_link(symbol)}"

    return "\n".join(
        [
            header,
            time_line,
            underlying_line,
            separator,
            order_line,
            money_line,
            tags_line,
            context_line_fmt,
            bias_line_fmt,
            chart_line,
        ]
    )


def format_unusual_option_alert(
    *,
    contract: OptionContract,
    flow_tags: Optional[list[str]] = None,
    volume_today: Optional[int] = None,
    avg_volume: Optional[int] = None,
    trade_size: Optional[int] = None,
    chart_symbol: Optional[str] = None,
    narrative: Optional[str] = None,
) -> str:
    """Premium unusual-flow alert with parsed contract + context."""

    now = datetime.now(eastern)
    timestamp = now.strftime("%m-%d-%Y · %I:%M %p EST")
    symbol = (chart_symbol or contract.symbol or "?").upper()

    change_pct = _underlying_change_pct(contract)
    change_text = f" ({change_pct:+.1f}% today)" if change_pct is not None else ""
    rvol_text = (
        f" · RVOL {contract.underlying_rvol:.1f}×" if contract.underlying_rvol is not None else ""
    )
    underlying_line = f"💰 Underlying: {_format_currency(contract.underlying_price)}{change_text}{rvol_text}"

    contract_brief = format_contract_brief_with_size(contract)
    premium_text = _format_currency(contract.premium)
    notional_text = _format_currency(contract.notional, decimals=0)

    vol_today_val = volume_today if volume_today is not None else contract.volume
    vol_today_text = f"{vol_today_val:,}" if vol_today_val is not None else "n/a"
    avg_text = f"{avg_volume:,}" if avg_volume is not None else "n/a"

    size_val = trade_size if trade_size is not None else contract.size
    size_text = str(size_val) if size_val is not None else "n/a"

    share_text = "n/a"
    if vol_today_val and size_val:
        share = (size_val / vol_today_val) * 100
        share_text = f"{share:.1f}% of today’s option volume"

    oi_val = contract.open_interest or 0
    ratio_text = "n/a"
    if oi_val and vol_today_val:
        ratio_text = f"{vol_today_val/oi_val:.1f}× OI"
    elif vol_today_val:
        ratio_text = "volume present, no OI data"

    flow_tag_text = " · ".join(flow_tags) if flow_tags else "n/a"
    narrative_line = narrative or "Short-dated flow well above normal activity."

    header = f"⚠️ UNUSUAL FLOW — {symbol}"
    time_line = f"🕒 {timestamp}"
    separator = "────────────"
    order_line = f"🎯 Order: {contract_brief}"
    money_line = f"💵 Premium per contract: {premium_text} · Total Notional: {notional_text}"
    unusual_header = "📊 Unusual vs normal:"
    volume_line = f"• Option volume today: {vol_today_text} (avg {avg_text})"
    trade_line = f"• This trade: {size_text} contracts ({share_text})"
    oi_line = f"• Volume vs OI: {vol_today_text} vs {oi_val} ({ratio_text})"
    tags_line = f"🧠 Flow tags: {flow_tag_text}"
    narrative_fmt = f"📌 Narrative: {narrative_line}"
    chart_line = f"🔗 Chart: {chart_link(symbol)}"

    return "\n".join(
        [
            header,
            time_line,
            underlying_line,
            separator,
            order_line,
            money_line,
            unusual_header,
            volume_line,
            trade_line,
            oi_line,
            tags_line,
            narrative_fmt,
            chart_line,
        ]
    )


def format_cheap_option_alert(
    *,
    contract: OptionContract,
    premium_cap: float,
    min_notional: float,
    min_size: int,
    chart_symbol: Optional[str] = None,
    narrative: Optional[str] = None,
) -> str:
    """Lottery-style cheap-flow alert with thresholds and context."""

    now = datetime.now(eastern)
    timestamp = now.strftime("%m-%d-%Y · %I:%M %p EST")
    symbol = (chart_symbol or contract.symbol or "?").upper()

    change_pct = _underlying_change_pct(contract)
    change_text = f" ({change_pct:+.1f}% today)" if change_pct is not None else ""
    underlying_line = f"💵 Underlying: {_format_currency(contract.underlying_price)}{change_text}"

    contract_brief = format_contract_brief_with_size(contract)
    premium_text = _format_currency(contract.premium)
    notional_text = _format_currency(contract.notional, decimals=0)
    size_text = str(contract.size) if contract.size is not None else "n/a"

    premium_note = f"within CHEAP_MAX_PREMIUM=${premium_cap:.2f}"
    notional_note = "meets CHEAP_MIN_NOTIONAL" if contract.notional and contract.notional >= min_notional else "notional n/a"
    size_note = "meets CHEAP_MIN_SIZE" if contract.size and contract.size >= min_size else "size n/a"

    dte_text = f"{contract.dte} DTE" if contract.dte is not None else "n/a"

    # Simple structure: near/short dated + moneyness + size
    structure_bits: list[str] = []
    if contract.dte is not None:
        if contract.dte <= 21:
            structure_bits.append("near-dated")
        elif contract.dte <= 60:
            structure_bits.append("mid-term")
        else:
            structure_bits.append("far-dated")
    parsed = _parse_occ(contract.contract)
    if contract.strike is not None and contract.underlying_price:
        cp_source = contract.cp or parsed.get("cp")
        cp_letter = "C" if cp_source == "CALL" else "P" if cp_source == "PUT" else None
        if cp_letter == "C":
            structure_bits.append("OTM call" if contract.strike > contract.underlying_price else "ITM/ATM call")
        elif cp_letter == "P":
            structure_bits.append("OTM put" if contract.strike < contract.underlying_price else "ITM/ATM put")
    structure_bits.append(f"sized at {size_text} contracts" if contract.size else "size unknown")
    structure_line = " · ".join(structure_bits)

    oi_val = contract.open_interest or 0
    vol_val = contract.volume or 0
    ratio_text = "n/a"
    if oi_val and vol_val:
        ratio_text = f"{vol_val/oi_val:.1f}× OI"
    elif vol_val:
        ratio_text = "volume present, no OI data"

    context_line = f"Option volume {vol_val} vs OI {oi_val} ({ratio_text})"
    bias = narrative or "Speculative bullish \"lottery\" flow"

    header = f"💰 CHEAP FLOW — {symbol}"
    time_line = f"🕒 {timestamp}"
    separator = "────────────"
    order_line = f"🎯 Order: {contract_brief}"
    premium_line = f"💸 Premium per contract: {premium_text} ({premium_note})"
    notional_line = f"💰 Total Notional: {notional_text} ({notional_note}; {size_note})"
    structure_header = f"📊 Structure: {structure_line}" if structure_line else "📊 Structure: n/a"
    context_fmt = f"⚖️ Context: {context_line}"
    bias_line = f"🧠 Bias: {bias}"
    dte_line = f"⏳ Tenor: {dte_text}" if contract.dte is not None else None
    chart_line = f"🔗 Chart: {chart_link(symbol)}"

    lines = [
        header,
        time_line,
        underlying_line,
        separator,
        order_line,
        premium_line,
        notional_line,
        structure_header,
        context_fmt,
        bias_line,
    ]
    if dte_line:
        lines.insert(5, dte_line)
    lines.append(chart_line)

    return "\n".join(lines)

def format_option_alert(
    *,
    emoji: str,
    label: str,
    contract: OptionContract,
    iv_line: Optional[str] = None,
    chart_symbol: Optional[str] = None,
) -> str:
    """Return a human-readable option alert body used by all option flow bots."""

    now = datetime.now(eastern)
    timestamp = now.strftime("%m-%d-%Y · %I:%M %p EST")
    dte_text = f"{contract.dte} DTE" if contract.dte is not None else "n/a"
    premium_text = _format_currency(contract.premium)
    size_text = str(contract.size) if contract.size is not None else "n/a"
    notional_text = _format_currency(contract.notional, decimals=0)
    underlying_text = _format_currency(contract.underlying_price)
    iv_value = f"{contract.iv:.1f}%" if contract.iv is not None else "n/a"
    volume_text = str(contract.volume) if contract.volume is not None else "n/a"
    oi_text = (
        str(contract.open_interest)
        if contract.open_interest is not None
        else "n/a"
    )
    iv_display = iv_line if iv_line is not None else f"IV: {iv_value} | Volume: {volume_text} | OI: {oi_text}"

    header_symbol = (chart_symbol or contract.symbol or "?").upper()
    header = f"{emoji} {label} — {header_symbol} ({timestamp})"
    separator = "────────────"
    contract_line = (
        f"• Contract: {format_option_contract_display(contract)} (⏳ {dte_text})"
    )
    underlying_line = f"• 💵 Underlying: {underlying_text}"
    money_line = (
        f"• 💰 Premium: {premium_text} | Size: {size_text} | Notional: {notional_text}"
    )
    iv_line_fmt = f"• 📊 {iv_display}"
    chart_line = f"• 📈 Chart: {chart_link(header_symbol)}"

    return "\n".join(
        [
            header,
            separator,
            contract_line,
            underlying_line,
            money_line,
            iv_line_fmt,
            chart_line,
        ]
    )


def send_option_alert(text: str) -> None:
    """Send the formatted option alert via Telegram."""

    send_alert_text(text)
