# bots/options_flow.py
"""Options flow scanner.

Scans a universe of underlyings, pulls option chain snapshots from the data
provider, and emits flow buckets (CHEAP / UNUSUAL / WHALE / IVCRUSH).

The bot is gated to regular trading hours by default but can be forced to run
outside RTH via the ``OPTIONS_FLOW_ALLOW_OUTSIDE_RTH`` env for debugging.
"""

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .shared import (
    DEBUG_FLOW_REASONS,
    chart_link,
    debug_filter_reason,
    get_last_option_trades_cached,
    get_option_chain_cached,
    in_rth_window_est,
    now_est,
    record_bot_stats,
    resolve_universe_for_bot,
    send_alert,
)
# ---------------- ENV / CONFIG ----------------

OPTIONS_FLOW_TICKER_UNIVERSE = os.getenv("OPTIONS_FLOW_TICKER_UNIVERSE")
OPTIONS_FLOW_MAX_UNIVERSE = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", "2000"))
ALLOW_OUTSIDE_RTH = os.getenv("OPTIONS_FLOW_ALLOW_OUTSIDE_RTH", "false").lower() == "true"

OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5.0"))

CHEAP_MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "0.60"))
CHEAP_MIN_SIZE = int(os.getenv("CHEAP_MIN_SIZE", "5"))
CHEAP_MIN_NOTIONAL = float(os.getenv("CHEAP_MIN_NOTIONAL", "3000"))

UNUSUAL_MIN_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "20"))
UNUSUAL_MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "15000"))
UNUSUAL_MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))

WHALES_MIN_SIZE = int(os.getenv("WHALES_MIN_SIZE", "50"))
WHALES_MIN_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "150000"))
WHALES_MAX_DTE = int(os.getenv("WHALES_MAX_DTE", "120"))

IVCRUSH_MIN_IV_DROP_PCT = float(os.getenv("IVCRUSH_MIN_IV_DROP_PCT", "25.0"))
IVCRUSH_MIN_VOL = int(os.getenv("IVCRUSH_MIN_VOL", "100"))
IVCRUSH_MAX_DTE = int(os.getenv("IVCRUSH_MAX_DTE", "21"))

OPTIONS_IV_CACHE_PATH = os.getenv("OPTIONS_IV_CACHE_PATH", "/tmp/options_iv_cache.json")

# ---------------- DATA STRUCTURES ----------------


@dataclass
class OptionFlowRecord:
    symbol: str
    contract: str
    category: str  # CHEAP | UNUSUAL | WHALE | IVCRUSH
    direction: str  # CALL / PUT / MIXED / UNKNOWN
    price: float
    size: int
    notional: float
    dte: int
    expiry: str
    cp: str
    reasons: List[str]


# ---------------- HELPERS ----------------


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


def _parse_option_details(opt: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Try to pull expiry, contract ticker, and DTE from the snapshot object.
    We expect something like:

      opt["details"]["ticker"] -> "O:TSLA240118C00255000"
      opt["details"]["expiration_date"] -> "2024-01-18"

    Massive / Polygon sometimes vary naming slightly; we guard for that.
    """
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

    dte = None
    if expiry:
        try:
            # expiry like "2024-01-18" or "20240118"
            fmt = "%Y-%m-%d" if "-" in expiry else "%Y%m%d"
            dt_exp = datetime.strptime(expiry, fmt).date()
            today = datetime.now().date()
            dte = (dt_exp - today).days
        except Exception:
            dte = None

    return expiry, contract, dte


def _contract_type(contract: str) -> Optional[str]:
    """
    Crudely detect CALL/PUT from contract symbol (polygon / massive style).
      • Contains "C" before strike -> CALL
      • Contains "P" before strike -> PUT
    """
    if not contract:
        return None
    try:
        base = contract.replace("O:", "")
        if "C" in base and "P" not in base:
            return "CALL"
        if "P" in base and "C" not in base:
            return "PUT"
    except Exception:
        pass
    return None


def _categorize(
    price: float,
    size: int,
    notional: float,
    dte: int,
) -> Tuple[Optional[str], List[str]]:
    """
    Decide which primary category this flow belongs to.
    """
    reasons: List[str] = []
    category: Optional[str] = None

    # CHEAP: small premium, but decent size & notional
    if price <= CHEAP_MAX_PREMIUM and size >= CHEAP_MIN_SIZE and notional >= CHEAP_MIN_NOTIONAL:
        category = "CHEAP"
        reasons.append("cheap_premium")

    # UNUSUAL: large, short-dated
    if (
        dte is not None
        and 0 <= dte <= UNUSUAL_MAX_DTE
        and size >= UNUSUAL_MIN_SIZE
        and notional >= UNUSUAL_MIN_NOTIONAL
    ):
        # If already CHEAP, flag both
        category = category or "UNUSUAL"
        reasons.append("unusual_size_notional")

    # WHALES: very large size + notional, more relaxed DTE
    if (
        dte is not None
        and 0 <= dte <= WHALES_MAX_DTE
        and size >= WHALES_MIN_SIZE
        and notional >= WHALES_MIN_NOTIONAL
    ):
        category = category or "WHALE"
        reasons.append("whale_size_notional")

    return category, reasons


def _maybe_iv_crush(opt: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Very rough IV-crush detection using snapshot IV + day stats (if available).
    """
    reasons: List[str] = []
    iv = _safe_float(opt.get("implied_volatility") or opt.get("iv"))
    day = opt.get("day") or {}
    vol = _safe_int(day.get("volume") or day.get("v"))
    if iv is None or vol is None:
        return False, reasons

    iv_open = _safe_float(day.get("implied_volatility_open") or day.get("iv_open") or day.get("ivOpen"))
    if iv_open is None or iv_open <= 0:
        return False, reasons

    drop_pct = 100.0 * (iv_open - iv) / iv_open
    if drop_pct >= IVCRUSH_MIN_IV_DROP_PCT and vol >= IVCRUSH_MIN_VOL:
        reasons.append(f"iv_drop≈{drop_pct:.1f}%")
        return True, reasons

    return False, reasons


def _resolve_universe() -> List[str]:
    """
    Universe for options flow:
      1) OPTIONS_FLOW_TICKER_UNIVERSE env if set.
      2) Else TICKER_UNIVERSE/FALLBACK_TICKER_UNIVERSE via shared helper.
    """
    if OPTIONS_FLOW_TICKER_UNIVERSE:
        syms = [s.strip().upper() for s in OPTIONS_FLOW_TICKER_UNIVERSE.split(",") if s.strip()]
        print(
            f"[options_flow] Using OPTIONS_FLOW_TICKER_UNIVERSE with {len(syms)} symbols "
            f"(capped to {OPTIONS_FLOW_MAX_UNIVERSE})."
        )
        return syms[:OPTIONS_FLOW_MAX_UNIVERSE]

    universe = resolve_universe_for_bot("options_flow", max_tickers=OPTIONS_FLOW_MAX_UNIVERSE)
    universe = universe[:OPTIONS_FLOW_MAX_UNIVERSE]
    print(
        f"[options_flow] Using dynamic universe with {len(universe)} symbols (max "
        f"{OPTIONS_FLOW_MAX_UNIVERSE})."
    )
    return universe


# ---------------- MAIN BOT ----------------


async def run_options_flow() -> None:
    """
    Scan a universe of underlyings for interesting options flow using
    Massive/Polygon snapshot chains plus the last-trade endpoint as a fallback.
    """
    start_unix = time.time()

    if not ALLOW_OUTSIDE_RTH and not in_rth_window_est():
        print(
            "[options_flow] outside RTH window; skipping run (set "
            "OPTIONS_FLOW_ALLOW_OUTSIDE_RTH=true to debug)."
        )
        record_bot_stats("Options Flow", 0, 0, 0, 0.0)
        return

    universe = _resolve_universe()
    if not universe:
        print("[options_flow] universe empty; nothing to scan.")
        record_bot_stats("Options Flow", 0, 0, 0, 0.0)
        return

    scanned = 0
    matched_contracts: List[OptionFlowRecord] = []
    alerts_sent = 0
    reason_counts: Optional[Dict[str, int]] = {} if DEBUG_FLOW_REASONS else None

    def _log_reason(symbol: str, reason: str) -> None:
        debug_filter_reason("options_flow", symbol, reason)
        if reason_counts is not None:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for sym in universe:
        scanned += 1
        try:
            chain = get_option_chain_cached(sym, ttl_seconds=60)
            if not chain:
                _log_reason(sym, "no chain snapshot")
                continue

            if not isinstance(chain, dict):
                _log_reason(sym, "bad_chain_data")
                continue

            results = chain.get("results") or chain.get("options") or []
            if not isinstance(results, list) or not results:
                _log_reason(sym, "empty chain results")
                continue

            underlying = chain.get("underlying") or chain.get("underlying_asset") or {}
            last_under = underlying.get("last") or underlying.get("last_trade") or {}
            under_price = _safe_float(
                last_under.get("price")
                or last_under.get("p")
                or underlying.get("last_price")
                or underlying.get("price")
                or underlying.get("close")
            )
            if under_price is None or under_price < OPTIONS_MIN_UNDERLYING_PRICE:
                _log_reason(sym, f"underlying price {under_price} < {OPTIONS_MIN_UNDERLYING_PRICE}")
                continue

            for opt in results:
                if not isinstance(opt, dict):
                    _log_reason(sym, "non-dict option record")
                    continue

                expiry, contract, dte = _parse_option_details(opt)
                if not contract:
                    _log_reason(sym, "no contract symbol in snapshot")
                    continue

                last_trade_obj = (
                    opt.get("last")
                    or opt.get("last_trade")
                    or opt.get("lastTrade")
                    or {}
                )

                price = _safe_float(
                    (last_trade_obj.get("price") if isinstance(last_trade_obj, dict) else None)
                    or (last_trade_obj.get("p") if isinstance(last_trade_obj, dict) else None)
                )
                size = _safe_int(
                    (last_trade_obj.get("size") if isinstance(last_trade_obj, dict) else None)
                    or (last_trade_obj.get("s") if isinstance(last_trade_obj, dict) else None)
                )

                if price is None or size is None:
                    trade = get_last_option_trades_cached(contract)
                    if not trade:
                        _log_reason(sym, f"no last trade for {contract}")
                        continue

                    t_res = trade.get("results")
                    if isinstance(t_res, list) and t_res:
                        last = t_res[0]
                    elif isinstance(t_res, dict):
                        last = t_res
                    else:
                        last = trade

                    if isinstance(last, dict):
                        price = _safe_float(last.get("price") or last.get("p"))
                        size = _safe_int(last.get("size") or last.get("s"))

                if price is None or size is None:
                    _log_reason(sym, f"missing price/size for {contract}")
                    continue
                if price <= 0 or size <= 0:
                    _log_reason(sym, f"non-positive price/size for {contract}")
                    continue

                notional = price * size * 100.0

                cp = _contract_type(contract) or "UNKNOWN"

                if dte is None or dte < 0:
                    _log_reason(sym, f"bad dte={dte} for {contract}")
                    continue
                if dte > WHALES_MAX_DTE:
                    _log_reason(sym, f"dte={dte} beyond whales max={WHALES_MAX_DTE}")
                    continue

                category, reasons = _categorize(price, size, notional, dte)

                iv_hit, iv_reasons = _maybe_iv_crush(opt)
                if iv_hit:
                    reasons.extend(iv_reasons)
                    if not category:
                        category = "IVCRUSH"

                if not category:
                    _log_reason(sym, f"no category for {contract}")
                    continue

                record = OptionFlowRecord(
                    symbol=sym,
                    contract=contract,
                    category=category,
                    direction=cp,
                    price=price,
                    size=size,
                    notional=notional,
                    dte=dte,
                    expiry=expiry or "N/A",
                    cp=cp,
                    reasons=reasons,
                )
                matched_contracts.append(record)
        except Exception as e:
            _log_reason(sym, f"exception {e}")
            print(f"[options_flow] Error on {sym}: {e}")
            continue

    def _fmt_record(rec: OptionFlowRecord) -> str:
        direction = "BULL" if rec.cp == "CALL" else "BEAR" if rec.cp == "PUT" else "UNKNOWN"
        emoji = {
            "CHEAP": "🎲",
            "UNUSUAL": "🕵️",
            "WHALE": "🐋",
            "IVCRUSH": "💥",
        }.get(rec.category, "📈")

        reasons_str = " • ".join(rec.reasons) if rec.reasons else "flow_detected"
        chart = chart_link(rec.symbol)

        return (
            f"{emoji} {rec.category}\n"
            f"🕒 {now_est()}\n"
            f"📌 Contract: {rec.contract}\n"
            f"📦 Size: {rec.size}\n"
            f"💰 Notional: ≈ ${rec.notional:,.0f}\n"
            f"🗓️ DTE: {rec.dte} (exp {rec.expiry})\n"
            f"📈 Direction: {direction}\n"
            f"🔍 Reasons: {reasons_str}\n"
            f"🔗 Chart: {chart}"
        )

    for rec in matched_contracts:
        text = _fmt_record(rec)
        send_alert(
            bot_name="Options Flow",
            symbol=rec.symbol,
            last_price=rec.price,
            rvol=0.0,
            extra=text,
        )
        alerts_sent += 1

    run_seconds = time.time() - start_unix

    if reason_counts is not None and not matched_contracts:
        print(f"[options_flow] No alerts. Filter breakdown: {reason_counts}")

    print(
        f"[options_flow] done. scanned={scanned} matched={len(matched_contracts)} "
        f"alerts={alerts_sent} run_seconds={run_seconds:.2f}"
    )

    record_bot_stats("Options Flow", scanned, len(matched_contracts), alerts_sent, run_seconds)
