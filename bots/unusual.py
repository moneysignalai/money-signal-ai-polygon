import os
from datetime import date
from typing import List, Optional

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    send_alert,
    get_dynamic_top_volume_universe,
    describe_option_flow,
    chart_link,
    is_etf_blacklisted,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_OPTION_CONTRACT_VOLUME = int(os.getenv("MIN_OPTION_CONTRACT_VOLUME", "2000"))
MAX_CONTRACTS_PER_UNDERLYING = int(os.getenv("MAX_CONTRACTS_PER_UNDERLYING", "40"))
MIN_UNUSUAL_NOTIONAL = float(os.getenv("MIN_UNUSUAL_NOTIONAL", "200000"))  # $200k default


def _get_ticker_universe() -> List[str]:
    env = os.getenv("TICKER_UNIVERSE")
    if env:
        return [t.strip().upper() for t in env.split(",") if t.strip()]
    return get_dynamic_top_volume_universe(max_tickers=100, volume_coverage=0.90)


def _safe_parse_date(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    try:
        return date.fromisoformat(d)
    except Exception:
        return None


def _decode_contract_pretty(sym: str, contract_ticker: str, exp: Optional[date], side: str, strike: float) -> str:
    if not exp:
        return f"{contract_ticker} ({side} {strike:.2f})"
    try:
        pretty_date = exp.strftime("%m/%d/%Y")
        side_letter = side[0].upper() if side else "?"
        return f"{sym} {pretty_date:.10s} {strike:.2f} {side_letter}"
    except Exception:
        return f"{contract_ticker} ({side} {strike:.2f})"


def _scan_unusual_for_symbol(sym: str):
    if not _client:
        return []

    today = date.today()
    today_s = today.isoformat()

    try:
        last_trade = _client.get_last_trade(ticker=sym)
        underlying_px = float(last_trade.price)
    except Exception as e:
        print(f"[unusual] get_last_trade failed for {sym}: {e}")
        return []

    try:
        contracts_gen = _client.list_options_contracts(
            underlying_ticker=sym,
            as_of=today_s,
            limit=1000,
        )
    except Exception as e:
        print(f"[unusual] list_options_contracts failed for {sym}: {e}")
        return []

    alerts = []
    count = 0
    for c in contracts_gen:
        if count >= MAX_CONTRACTS_PER_UNDERLYING:
            break
        count += 1

        exp = _safe_parse_date(getattr(c, "expiration_date", None))
        if not exp:
            continue

        dte = (exp - today).days
        if dte < 0 or dte > 7:  # short-dated sweeps only
            continue

        strike = float(getattr(c, "strike_price", 0.0) or 0.0)
        if strike <= 0 or underlying_px <= 0:
            continue

        moneyness = abs(strike - underlying_px) / underlying_px
        if moneyness > 0.20:
            continue

        contract_ticker = getattr(c, "ticker", None)
        if not contract_ticker:
            continue

        # Contract tape
        try:
            bars = list(
                _client.list_aggs(
                    ticker=contract_ticker,
                    multiplier=1,
                    timespan="minute",
                    from_=today_s,
                    to=today_s,
                    limit=5_000,
                )
            )
        except Exception as e:
            print(f"[unusual] list_aggs failed for {contract_ticker}: {e}")
            continue

        if not bars:
            continue

        vol_today = float(sum(b.volume for b in bars))
        if vol_today < MIN_OPTION_CONTRACT_VOLUME:
            continue

        total_prem = float(sum(b.close * b.volume for b in bars))
        avg_price = total_prem / max(vol_today, 1.0)
        notional = total_prem * 100.0

        # Core spec: $200k+ in the SAME contract
        if notional < MIN_UNUSUAL_NOTIONAL:
            continue

        side = getattr(c, "contract_type", "unknown").upper()
        pretty_name = _decode_contract_pretty(sym, contract_ticker, exp, side, strike)

        # ITM / OTM tag
        if side.startswith("C"):
            itm = underlying_px > strike
        elif side.startswith("P"):
            itm = underlying_px < strike
        else:
            itm = False
        itm_tag = "ITM" if itm else "OTM"

        moneyness_pct = moneyness * 100.0
        flow_desc = describe_option_flow(side, dte, moneyness)

        extra = (
            f"🕵️ Unusual {side} sweep: {pretty_name}\n"
            f"📌 Flow Type: {flow_desc}\n"
            f"⏱ DTE: {dte} · {itm_tag} · Moneyness {moneyness_pct:.1f}%\n"
            f"📦 Volume: {int(vol_today):,} contracts · Avg: ${avg_price:.2f}\n"
            f"💰 Notional: ≈ ${notional:,.0f}\n"
            f"🔗 Chart: {chart_link(sym)}"
        )

        alerts.append((sym, underlying_px, 0.0, extra))

    return alerts


async def run_unusual():
    """
    Unusual Options Sweeps:
      • Short-dated (<=7 DTE)
      • Near-the-money (<=20% from spot)
      • Volume above MIN_OPTION_CONTRACT_VOLUME
      • Notional >= MIN_UNUSUAL_NOTIONAL (default $200k)
      • No explicit time-window guard → "all day"
    """
    if not POLYGON_KEY:
        print("[unusual] POLYGON_KEY not set; skipping scan.")
        return

    universe = _get_ticker_universe()
    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        try:
            alerts = _scan_unusual_for_symbol(sym)
        except Exception as e:
            print(f"[unusual] scan failed for {sym}: {e}")
            continue

        for _sym, last_price, rvol, extra in alerts:
            send_alert("unusual", _sym, last_price, 0.0, extra=extra)