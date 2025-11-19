import os
from datetime import date
from typing import List, Optional

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_OPTION_CONTRACT_VOLUME = int(os.getenv("MIN_OPTION_CONTRACT_VOLUME", "5000"))
MAX_CONTRACTS_PER_UNDERLYING = int(os.getenv("MAX_CONTRACTS_PER_UNDERLYING", "40"))


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
    """
    Try to turn "O:ACHR251121C00009000" into something like:
      "ACHR 11/21/2025 9.00 C"
    """
    if not exp:
        return f"{contract_ticker} ({side} {strike:.2f})"
    try:
        pretty_date = exp.strftime("%m/%d/%Y")
        side_letter = side[0].upper() if side else "?"
        return f"{sym} {pretty_date} {strike:.2f} {side_letter}"
    except Exception:
        return f"{contract_ticker} ({side} {strike:.2f})"


def _scan_unusual_for_symbol(sym: str):
    if not _client:
        return []

    today = date.today()
    today_s = today.isoformat()

    # Underlying last price
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

        days_to_exp = (exp - today).days
        if days_to_exp < 0 or days_to_exp > 7:
            continue

        strike = float(getattr(c, "strike_price", 0.0) or 0.0)
        if strike <= 0:
            continue

        # Near the money: ±20% band
        if underlying_px <= 0:
            continue
        moneyness = abs(strike - underlying_px) / underlying_px
        if moneyness > 0.20:
            continue

        contract_ticker = getattr(c, "ticker", None)
        if not contract_ticker:
            continue

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
        notional = total_prem * 100.0
        avg_price = total_prem / max(vol_today, 1.0)

        side = getattr(c, "contract_type", "unknown").upper()
        pretty_name = _decode_contract_pretty(sym, contract_ticker, exp, side, strike)

        # ITM / OTM
        if side.startswith("C"):
            itm = underlying_px > strike
        elif side.startswith("P"):
            itm = underlying_px < strike
        else:
            itm = False
        itm_tag = "ITM" if itm else "OTM"

        moneyness_pct = moneyness * 100.0

        extra = (
            f"🎯 {side} flow: {pretty_name}\n"
            f"⏱ DTE: {days_to_exp} · {itm_tag} · Moneyness {moneyness_pct:.1f}%\n"
            f"📦 Volume: {int(vol_today):,} contracts · Avg: ${avg_price:.2f}\n"
            f"💰 Notional: ≈ ${notional:,.0f}"
        )

        alerts.append((sym, underlying_px, 0.0, extra))

    return alerts


async def run_unusual():
    """
    Unusual options buyer scanner.
    """
    if not POLYGON_KEY:
        print("[unusual] POLYGON_KEY not set; skipping scan.")
        return

    universe = _get_ticker_universe()
    for sym in universe:
        try:
            alerts = _scan_unusual_for_symbol(sym)
        except Exception as e:
            print(f"[unusual] scan failed for {sym}: {e}")
            continue

        for _sym, last_price, rvol, extra in alerts:
            # rvol is per underlying; we don't compute it here → pass 0.0
            send_alert("unusual", _sym, last_price, 0.0, extra=extra)