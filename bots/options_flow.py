# bots/options_flow.py

import os
import time  # 🔹 ADD THIS LINE
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .shared import (
    now_est,
    in_rth_window_est,
    debug_filter_reason,
    resolve_universe_for_bot,
    get_option_chain_cached,
    get_last_option_trades_cached,
    record_bot_stats,
    chart_link,
    send_alert,
)
# ---------------- ENV / CONFIG ----------------

OPTIONS_FLOW_TICKER_UNIVERSE = os.getenv("OPTIONS_FLOW_TICKER_UNIVERSE")
OPTIONS_FLOW_MAX_UNIVERSE = int(os.getenv("OPTIONS_FLOW_MAX_UNIVERSE", "500"))

OPTIONS_MIN_UNDERLYING_PRICE = float(os.getenv("OPTIONS_MIN_UNDERLYING_PRICE", "5.0"))

CHEAP_MAX_PREMIUM = float(os.getenv("CHEAP_MAX_PREMIUM", "0.75"))
CHEAP_MIN_SIZE = int(os.getenv("CHEAP_MIN_SIZE", "20"))
CHEAP_MIN_NOTIONAL = float(os.getenv("CHEAP_MIN_NOTIONAL", "5000"))

UNUSUAL_MIN_SIZE = int(os.getenv("UNUSUAL_MIN_SIZE", "50"))
UNUSUAL_MIN_NOTIONAL = float(os.getenv("UNUSUAL_MIN_NOTIONAL", "75000"))
UNUSUAL_MAX_DTE = int(os.getenv("UNUSUAL_MAX_DTE", "45"))

WHALES_MIN_SIZE = int(os.getenv("WHALES_MIN_SIZE", "75"))
WHALES_MIN_NOTIONAL = float(os.getenv("WHALES_MIN_NOTIONAL", "300000"))
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
    contract = details.get("ticker") or opt.get("ticker")
    expiry = details.get("expiration_date") or details.get("expiry") or details.get("expiration")

    dte = None
    if expiry:
        try:
            # expiry like "2024-01-18"
            from datetime import datetime as _dt

            dt_exp = _dt.strptime(expiry, "%Y-%m-%d").date()
            today = now_est().date()
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
        return syms[:OPTIONS_FLOW_MAX_UNIVERSE]
    return resolve_universe_for_bot("options_flow", max_tickers=OPTIONS_FLOW_MAX_UNIVERSE)


# ---------------- MAIN BOT ----------------


async def run_options_flow() -> None:
    """
    Scan a universe of underlyings for interesting options flow using
    Massive/Polygon snapshot chains plus the last-trade endpoint as a fallback.
    """
    start_ts = now_est()
    start_unix = time.time()

    import time as _time

    if not in_rth_window_est():
        # Only run during RTH; options flow outside RTH is less useful.
        print("[options_flow] outside RTH window; skipping run.")
        record_bot_stats("Options Flow", 0, 0, 0, 0.0)
        return

    universe = _resolve_universe()
    print(f"[options_flow] universe size={len(universe)}")

    scanned = 0
    matched_contracts: List[OptionFlowRecord] = []
    alerts_sent = 0

    for sym in universe:
        scanned += 1

        # Snapshots sometimes use lowercase; normalize
        chain = get_option_chain_cached(sym, ttl_seconds=60)
        if not chain:
            debug_filter_reason("options_flow", sym, "no chain snapshot")
            continue

        results = chain.get("results") or chain.get("options") or []
        if not isinstance(results, list) or not results:
            debug_filter_reason("options_flow", sym, "empty chain results")
            continue

        # Underlying snapshot may help with reference price
        underlying = chain.get("underlying") or {}
        under_price = _safe_float(
            (underlying.get("last") or {}).get("price")
            or underlying.get("last_price")
            or underlying.get("price")
        )
        if under_price is None or under_price < OPTIONS_MIN_UNDERLYING_PRICE:
            debug_filter_reason(
                "options_flow",
                sym,
                f"underlying price {under_price} < {OPTIONS_MIN_UNDERLYING_PRICE}",
            )
            continue

        for opt in results:
            if not isinstance(opt, dict):
                continue

            expiry, contract, dte = _parse_option_details(opt)
            if not contract:
                debug_filter_reason("options_flow", sym, "no contract symbol in snapshot")
                continue

            # -------- Last trade: prefer snapshot last_trade, then fallback to /v2/last/trade --------

            # Prefer the embedded snapshot `last_trade` from Massive/Polygon's option-chain
            # snapshot to avoid per-contract HTTP calls and to stay aligned with the
            # current v3 snapshot schema. If that is missing (plan limitations, illiquid
            # contract, etc), fall back to the dedicated last-trade endpoint.
            last_trade_obj = (opt.get("last_trade") or opt.get("lastTrade") or {}) if isinstance(
                opt, dict
            ) else {}

            price = _safe_float(last_trade_obj.get("price") or last_trade_obj.get("p"))
            size = _safe_int(last_trade_obj.get("size") or last_trade_obj.get("s"))

            if price is None or size is None:
                trade = get_last_option_trades_cached(contract)
                if not trade:
                    debug_filter_reason("options_flow", sym, f"no last trade for {contract}")
                    continue

                t_res = trade.get("results")
                if isinstance(t_res, list) and t_res:
                    last = t_res[0]
                elif isinstance(t_res, dict):
                    last = t_res
                else:
                    # Some Polygon/Massive responses put the trade fields at the top level
                    last = trade

                if isinstance(last, dict):
                    price = _safe_float(last.get("price") or last.get("p"))
                    size = _safe_int(last.get("size") or last.get("s"))

            if price is None or size is None:
                debug_filter_reason("options_flow", sym, f"missing price/size for {contract}")
                continue
            if price <= 0 or size <= 0:
                debug_filter_reason("options_flow", sym, f"non-positive price/size for {contract}")
                continue

            notional = price * size * 100.0

            # -------- Resolve underlying, expiry, contract type --------

            cp = _contract_type(contract) or "UNKNOWN"

            # DTE guards
            if dte is None or dte < 0:
                debug_filter_reason("options_flow", sym, f"bad dte={dte} for {contract}")
                continue
            if dte > WHALES_MAX_DTE:
                debug_filter_reason("options_flow", sym, f"dte={dte} beyond whales max={WHALES_MAX_DTE}")
                continue

            # -------- Categorize flow (cheap / unusual / whales) --------

            category, reasons = _categorize(price, size, notional, dte)

            # IV Crush overlay (can stack with others)
            iv_hit, iv_reasons = _maybe_iv_crush(opt)
            if iv_hit:
                reasons.extend(iv_reasons)
                if not category:
                    category = "IVCRUSH"

            if not category:
                debug_filter_reason("options_flow", sym, f"no category for {contract}")
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

    # -------- Alert formatting --------

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
            f"{emoji} {rec.category} — {rec.symbol}\n"
            f"🕒 {now_est().strftime('%I:%M %p EST').lstrip('0')}\n"
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
        send_alert(text)
        alerts_sent += 1

    run_seconds = time.time() - start_unix
    print(
        f"[options_flow] done. scanned={scanned} matched={len(matched_contracts)} "
        f"alerts={alerts_sent} run_seconds={run_seconds:.2f}"
    )

    record_bot_stats("Options Flow", scanned, len(matched_contracts), alerts_sent, run_seconds)