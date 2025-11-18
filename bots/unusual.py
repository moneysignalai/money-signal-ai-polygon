import os
from datetime import date
from typing import List, Optional

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_OPTION_CONTRACT_VOLUME = int(os.getenv("MIN_OPTION_CONTRACT_VOLUME", "2000"))
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

        days_to_exp = (exp - today).days
        if days_to_exp < 0 or days_to_exp > 7:
            continue

        strike = float(getattr(c, "strike_price", 0.0) or 0.0)
        if strike <= 0:
            continue

        moneyness = abs(strike - underlying_px) / underlying_px
        if moneyness > 0.2:
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

        notional = float(sum(b.close * b.volume for b in bars)) * 100.0
        avg_price = notional / (vol_today * 100.0) if vol_today > 0 else 0.0

        side = getattr(c, "contract_type", "unknown").upper()
        extra = (
            f"Unusual {side} flow in {contract_ticker}\n"
            f"Underlying {sym} ≈ {underlying_px:.2f}\n"
            f"Strike {strike:.2f} · DTE {days_to_exp}\n"
            f"Volume {int(vol_today):,} contracts · Avg Price ${avg_price:.2f}\n"
            f"Notional ≈ ${notional:,.0f}"
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
            send_alert("unusual", _sym, last_price, rvol, extra=extra)