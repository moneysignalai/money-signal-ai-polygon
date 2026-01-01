"""Shared helpers for options flow bots.

Provides utilities to resolve option contract details from Massive/Polygon
snapshots so the individual option flow bots can focus on their filters.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from bots.shared import (
    chart_link,
    debug_filter_reason,
    eastern,
    get_last_option_trades_cached,
    get_option_chain_cached,
    send_alert_text,
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


def _extract_underlying_price(chain: Dict[str, Any]) -> Optional[float]:
    underlying = chain.get("underlying") or {}
    last = underlying.get("last") or {}
    return _safe_float(
        last.get("price")
        or underlying.get("last_price")
        or underlying.get("price")
        or underlying.get("close")
    )


def _option_iv(opt: Dict[str, Any]) -> Optional[float]:
    return _safe_float(
        opt.get("implied_volatility")
        or opt.get("iv")
        or (opt.get("greeks") or {}).get("iv")
    )


def iter_option_contracts(symbol: str, *, ttl_seconds: int = 60) -> List[OptionContract]:
    """Return parsed option contracts for an underlying symbol.

    Each contract includes price/size/notional when available. Errors are
    swallowed so callers can safely iterate without crashing.
    """

    chain = get_option_chain_cached(symbol, ttl_seconds=ttl_seconds) or {}
    options = chain.get("results") or chain.get("options") or []
    underlying_price = _extract_underlying_price(chain)

    contracts: List[OptionContract] = []
    for opt in options:
        expiry, contract, dte = _parse_option_details(opt)
        if not contract:
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
        premium = _safe_float(
            last_trade_obj.get("price")
            or last_trade_obj.get("p")
            or last_trade_obj.get("mid")
            or opt.get("last_price")
            or opt.get("price")
        )
        size = _safe_int(last_trade_obj.get("size") or last_trade_obj.get("s") or opt.get("size"))

        if (premium is None or size is None) and contract:
            # Try last trade fallback
            try:
                lt = get_last_option_trades_cached(contract)
                trade = lt.get("results") if isinstance(lt, dict) else None
                if trade:
                    premium = premium if premium is not None else _safe_float(trade.get("price"))
                    size = size if size is not None else _safe_int(trade.get("size") or trade.get("sip_timestamp"))
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
                underlying_price=underlying_price,
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


def _format_currency(value: Optional[float], *, decimals: int = 2) -> str:
    if value is None or value <= 0:
        return "N/A"
    return f"${value:,.{decimals}f}"


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
