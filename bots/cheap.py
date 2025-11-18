import os
from datetime import date
from typing import List, Optional

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import POLYGON_KEY, send_alert, get_dynamic_top_volume_universe

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None

# --------- ENV-TUNABLE FILTERS FOR CHEAP BOT ----------

# Minimum intraday option volume
MIN_CHEAP_OPTION_VOL = int(os.getenv("MIN_CHEAP_OPTION_VOL", "1000"))   # was 200

# Price band for "cheap" contracts
CHEAP_MIN_PRICE = float(os.getenv("CHEAP_MIN_PRICE", "0.10"))           # was 0.05
CHEAP_MAX_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "0.60"))           # was 1.00

# Max number of option contracts checked per underlying per scan
MAX_CONTRACTS_PER_UNDERLYING = int(os.getenv("CHEAP_MAX_CONTRACTS_PER_UNDERLYING", "40"))

# Max DTE (0–N). 0 = same-day expiry (0DTE), 1 = next day, etc.
CHEAP_MAX_DTE = int(os.getenv("CHEAP_MAX_DTE", "3"))

# How close to-the-money we want (0.10 = ±10% of underlying)
MAX_CHEAP_MONEYNESS = float(os.getenv("MAX_CHEAP_MONEYNESS", "0.12"))   # 12% band around spot

# Minimum notional traded in that contract (premium * volume * 100)
MIN_CHEAP_NOTIONAL = float(os.getenv("MIN_CHEAP_NOTIONAL", "75000"))    # $75k+ → more serious flow


# ------------------------------------------------------


def _get_ticker_universe() -> List[str]:
    """
    If TICKER_UNIVERSE is set, use that (comma-separated list).
    Otherwise use dynamic top-volume universe (covers ~90% of market volume).
    """
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
    Try to turn "O:BBAI251121P00006000" into something like:
      "BBAI 11/21/2025 6.00 P"

    If parsing fails, fall back to the raw Polygon ticker.
    """
    # Base: just a nice fallback
    if not exp:
        return f"{contract_ticker} ({side} {strike:.2f})"

    try:
        # exp is a date object already
        pretty_date = exp.strftime("%m/%d/%Y")
        side_letter = side[0].upper() if side else "?"
        return f"{sym} {pretty_date} {strike:.2f} {side_letter}"
    except Exception:
        return f"{contract_ticker} ({side} {strike:.2f})"


async def run_cheap():
    """
    Cheap 0DTE / short-dated (<= CHEAP_MAX_DTE) options scanner.

    Goals:
    - Focus on liquid, cheap premium contracts
    - Require meaningful notional (MIN_CHEAP_NOTIONAL)
    - Near-the-money (MAX_CHEAP_MONEYNESS band)
    - DTE <= CHEAP_MAX_DTE (default 3 days)
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
        # Underlying last price
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
            if dte < 0 or dte > CHEAP_MAX_DTE:
                continue

            contract_ticker = getattr(c, "ticker", None)
            if not contract_ticker:
                continue

            side = getattr(c, "contract_type", "unknown").upper()
            strike = float(getattr(c, "strike_price", 0.0) or 0.0)
            if strike <= 0:
                continue

            # Moneyness filter: ignore way-out-of-the-money lottery tickets
            if underlying_px <= 0:
                continue
            moneyness = abs(strike - underlying_px) / underlying_px
            if moneyness > MAX_CHEAP_MONEYNESS:
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

            # Volume-weighted average price
            vwap_numerator = sum(b.close * b.volume for b in bars)
            avg_px = float(vwap_numerator / max(vol_today, 1.0))

            # Enforce "cheap" price band
            if not (CHEAP_MIN_PRICE <= avg_px <= CHEAP_MAX_PRICE):
                continue

            notional = avg_px * vol_today * 100.0
            if notional < MIN_CHEAP_NOTIONAL:
                continue

            # Build a nicer contract label
            pretty_name = _decode_contract_pretty(sym, contract_ticker, exp, side, strike)

            # Label 0DTE vs N-day
            dte_label = "0DTE" if dte == 0 else f"{dte} DTE"

            # ITM / OTM tag
            if side.startswith("C"):
                itm = underlying_px > strike
            elif side.startswith("P"):
                itm = underlying_px < strike
            else:
                itm = False
            itm_tag = "ITM" if itm else "OTM"

            moneyness_pct = moneyness * 100.0

            extra = (
                f"Cheap {side} — {pretty_name}\n"
                f"{dte_label} · {itm_tag} · Moneyness {moneyness_pct:.1f}%\n"
                f"Underlying {sym} ≈ ${underlying_px:.2f} | Strike ${strike:.2f}\n"
                f"Avg Premium ${avg_px:.2f} · Volume {int(vol_today):,} · Notional ≈ ${notional:,.0f}"
            )

            # RVOL not specific for this contract, so pass 0.0
            send_alert("cheap", sym, underlying_px, 0.0, extra=extra)