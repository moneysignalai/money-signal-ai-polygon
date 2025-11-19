import os
from datetime import date, datetime
from typing import List, Optional

import pytz

try:
    from massive import RESTClient
except ImportError:
    from polygon import RESTClient

from bots.shared import (
    POLYGON_KEY,
    MIN_RVOL_GLOBAL,
    MIN_VOLUME_GLOBAL,
    send_alert,
    get_dynamic_top_volume_universe,
    describe_option_flow,
    chart_link,
    is_etf_blacklisted,
)

_client = RESTClient(api_key=POLYGON_KEY) if POLYGON_KEY else None
eastern = pytz.timezone("US/Eastern")

# --- Spec-based filters ---

CHEAP_UNDERLYING_MIN_PRICE = float(os.getenv("CHEAP_UNDERLYING_MIN_PRICE", "10.0"))
CHEAP_UNDERLYING_MAX_PRICE = float(os.getenv("CHEAP_UNDERLYING_MAX_PRICE", "80.0"))

CHEAP_MIN_PRICE = float(os.getenv("CHEAP_MIN_PRICE", "0.10"))   # option premium
CHEAP_MAX_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "0.60"))

CHEAP_MAX_DTE = int(os.getenv("CHEAP_MAX_DTE", "5"))            # 0–5 DTE
MAX_CONTRACTS_PER_UNDERLYING = int(os.getenv("CHEAP_MAX_CONTRACTS_PER_UNDERLYING", "40"))
MAX_CHEAP_MONEYNESS = float(os.getenv("MAX_CHEAP_MONEYNESS", "0.12"))

MIN_CHEAP_NOTIONAL = float(os.getenv("MIN_CHEAP_NOTIONAL", "75000"))  # $ notional
MIN_CHEAP_OPTION_VOL = int(os.getenv("MIN_CHEAP_OPTION_VOL", "1000"))


def _in_cheap_window() -> bool:
    """Cheap 0DTE Hunter: 9:30 AM – 4:00 PM EST."""
    now_et = datetime.now(eastern)
    minutes = now_et.hour * 60 + now_et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


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
        return f"{sym} {pretty_date} {strike:.2f} {side_letter}"
    except Exception:
        return f"{contract_ticker} ({side} {strike:.2f})"


async def run_cheap():
    """
    Cheap 0DTE Hunter:
      • Calls only
      • Underlying $10–$80
      • DTE 0–5
      • Cheap premium range (CHEAP_MIN_PRICE–CHEAP_MAX_PRICE)
      • High underlying RVOL + volume
      • Decent notional in the contract
      • 9:30–16:00 EST window
    """
    if not POLYGON_KEY:
        print("[cheap] POLYGON_KEY not set; skipping scan.")
        return
    if not _client:
        print("[cheap] Client not initialized; skipping scan.")
        return
    if not _in_cheap_window():
        print("[cheap] Outside 9:30–16:00 window; skipping scan.")
        return

    universe = _get_ticker_universe()
    today = date.today()
    today_s = today.isoformat()

    for sym in universe:
        if is_etf_blacklisted(sym):
            continue

        # 1) Get underlying last price
        try:
            last_trade = _client.get_last_trade(ticker=sym)
            underlying_px = float(last_trade.price)
        except Exception as e:
            print(f"[cheap] get_last_trade failed for {sym}: {e}")
            continue

        if not (CHEAP_UNDERLYING_MIN_PRICE <= underlying_px <= CHEAP_UNDERLYING_MAX_PRICE):
            continue

        # 2) Check underlying RVOL & day volume (high activity stocks only)
        try:
            days = list(
                _client.list_aggs(
                    ticker=sym,
                    multiplier=1,
                    timespan="day",
                    from_=(today.replace(day=max(1, today.day - 40))).isoformat(),
                    to=today_s,
                    limit=50,
                )
            )
        except Exception as e:
            print(f"[cheap] daily fetch failed for {sym}: {e}")
            continue

        if len(days) < 2:
            continue

        today_bar = days[-1]
        hist = days[:-1]
        if hist:
            recent = hist[-20:] if len(hist) > 20 else hist
            avg_vol = float(sum(d.volume for d in recent)) / len(recent)
        else:
            avg_vol = float(today_bar.volume)

        if avg_vol > 0:
            rvol_day = float(today_bar.volume) / avg_vol
        else:
            rvol_day = 1.0

        if rvol_day < MIN_RVOL_GLOBAL:
            continue

        day_vol = float(today_bar.volume)
        if day_vol < MIN_VOLUME_GLOBAL:
            continue

        # 3) Scan options contracts
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
            # Cheap 0DTE spec: calls only
            if not side.startswith("C"):
                continue

            strike = float(getattr(c, "strike_price", 0.0) or 0.0)
            if strike <= 0 or underlying_px <= 0:
                continue

            moneyness = abs(strike - underlying_px) / underlying_px
            if moneyness > MAX_CHEAP_MONEYNESS:
                continue

            # Minute bars for the option to compute volume + avg price
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

            vwap_numerator = sum(b.close * b.volume for b in bars)
            avg_px = float(vwap_numerator / max(vol_today, 1.0))

            # Cheap premium band
            if not (CHEAP_MIN_PRICE <= avg_px <= CHEAP_MAX_PRICE):
                continue

            notional = avg_px * vol_today * 100.0
            if notional < MIN_CHEAP_NOTIONAL:
                continue

            pretty_name = _decode_contract_pretty(sym, contract_ticker, exp, side, strike)

            moneyness_pct = moneyness * 100.0
            flow_desc = describe_option_flow(side, dte, moneyness)

            itm = underlying_px > strike  # calls only
            itm_tag = "ITM" if itm else "OTM"

            dte_label = "0DTE" if dte == 0 else f"{dte} DTE"

            extra = (
                f"💸 Cheap CALL — {pretty_name}\n"
                f"📌 Flow Type: {flow_desc}\n"
                f"⏱ {dte_label} · {itm_tag} · Moneyness {moneyness_pct:.1f}%\n"
                f"📦 Volume: {int(vol_today):,} · Avg Premium: ${avg_px:.2f}\n"
                f"💰 Notional: ≈ ${notional:,.0f}\n"
                f"🔗 Chart: {chart_link(sym)}"
            )

            send_alert("cheap", sym, underlying_px, rvol_day, extra=extra)