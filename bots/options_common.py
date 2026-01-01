"""Shared helpers for options flow bots.

Provides utilities to resolve option contract details from Massive/Polygon
snapshots so the individual option flow bots can focus on their filters.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from bots.shared import (
    debug_filter_reason,
    get_last_option_trades_cached,
    get_option_chain_cached,
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

        cp = opt.get("type") or opt.get("contract_type") or _contract_type(contract)
        strike = _safe_float(
            (opt.get("details") or {}).get("strike_price")
            or opt.get("strike_price")
            or opt.get("strike")
        )

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
                iv=_option_iv(opt),
                underlying_price=underlying_price,
            )
        )
    return contracts


def options_flow_allow_outside_rth() -> bool:
    return os.getenv("OPTIONS_FLOW_ALLOW_OUTSIDE_RTH", "false").lower() == "true"
