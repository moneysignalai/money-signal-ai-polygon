import os
from datetime import date
from typing import List, Optional

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

MIN_CHEAP_OPTION_VOL = int(os.getenv("MIN_CHEAP_OPTION_VOL", "200"))
CHEAP_MIN_PRICE = float(os.getenv("CHEAP_MIN_PRICE", "0.05"))
CHEAP_MAX_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "1.00"))
MAX_CONTRACTS_PER_UNDERLYING = int(os.getenv("CHEAP_MAX_CONTRACTS_PER_UNDERLYING", "60"))


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


async def run_cheap():
    """
    Cheap 0DTE / 3DTE options scanner.
    """
    if not POLYGON_KEY:
        print("[cheap] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[cheap] Client not initialized; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        try:
            last_trade = _client.get_last_trade(ticker=sym)
            underlying_px = float(last_trade.price)
        except Exception as e:
            print(f"[cheap] get_last_trade failed for {sym}: {e}")
            continue

        try:
            contracts_gen = _client.list_options_contracts(
                underlying_ticker=sym,
                as_of=today_s,
                limit=1000,
            )
        except Exception as e:
            print(f"[cheap] list_options_contracts failed for {sym}: {e}")
            continue

        count = 0
        for c in contracts_gen:
            if count >= MAX_CONTRACTS_PER_UNDERLYING:
                break
            count += 1

            exp = _safe_parse_date(getattr(c, "expiration_date", None))
            if not exp:
                continue
            dte = (exp - today).days
            if dte < 0 or dte > 3:
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
                print(f"[cheap] list_aggs failed for {contract_ticker}: {e}")
                continue

            if not bars:
                continue

            vol_today = float(sum(b.volume for b in bars))
            if vol_today < MIN_CHEAP_OPTION_VOL:
                continue

            avg_px = float(
                sum(b.close * b.volume for b in bars) / max(vol_today, 1.0)
            )

            if not (CHEAP_MIN_PRICE <= avg_px <= CHEAP_MAX_PRICE):
                continue

            side = getattr(c, "contract_type", "unknown").upper()
            strike = float(getattr(c, "strike_price", 0.0) or 0.0)
            notional = avg_px * vol_today * 100.0

            extra = (
                f"Cheap {side} {contract_ticker}\n"
                f"{sym} ≈ {underlying_px:.2f} | Strike {strike:.2f} | DTE {dte}\n"
                f"Avg Price ${avg_px:.2f} · Volume {int(vol_today):,} · Notional ≈ ${notional:,.0f}"
            )

            send_alert("cheap", sym, underlying_px, 0.0, extra=extra)