# Bot Contracts

> Snapshot of each bot's intended behavior, inputs, and guardrails. Values reflect current code defaults unless otherwise noted.

## premarket
- **Inputs**: Polygon daily + premarket minute bars via `RESTClient.list_aggs`. Earnings calendar via `bots.earnings` helpers.
- **Universe**: `resolve_universe_for_bot` dynamic equity universe (capped by `UNIVERSE_TOP_N` and `UNIVERSE_HARD_CAP`).
- **Window**: Premarket (EST) unless `PREMARKET_ALLOW_OUTSIDE_WINDOW=true`.
- **Triggers**: Large premarket gappers with volume and RVOL thresholds; rejects illiquid / low float names.
- **Alert schema**: Symbol, last price, RVOL, gap %, pre/post context; Telegram text via `send_alert_text`.
- **False positives**: Thin premarket quotes, one-off prints, news-driven spikes that retrace.
- **Dependencies**: `bots.shared` for universe, chart links, alerts; `bots.status_report` for stats.

## volume_monster
- **Inputs**: Polygon daily + intraday minute bars, grouped aggregates.
- **Universe**: Dynamic equity universe via `resolve_universe_for_bot` with RVOL floor.
- **Window**: RTH only.
- **Triggers**: Intraday volume/price surges relative to RVOL and dollar volume floors; looks for multi-timeframe confirmation.
- **Alert schema**: Symbol, last price, RVOL, volume spike summary; Telegram text with chart link.
- **False positives**: Opening-cross noise, block prints, news spikes without follow-through.
- **Dependencies**: `bots.shared` (RVOL floors, chart_link, send_alert_text), `bots.status_report` stats.

## gap_flow
- **Inputs**: Polygon daily + intraday minute bars.
- **Universe**: Dynamic equity universe capped by `GAP_FLOW_MAX_UNIVERSE`/`UNIVERSE_HARD_CAP`.
- **Window**: RTH (optional outside when env allows).
- **Triggers**: Gaps ≥ `_min_gap_pct` with dollar volume floor; intraday hold vs VWAP/gap range with RVOL filter.
- **Alert schema**: Gap %, intraday performance, VWAP relation, timestamps; Telegram text.
- **False positives**: Early fade after open, news halts, thin volume gaps.
- **Dependencies**: `bots.shared` (window helpers, universe, alerts), `bots.status_report`.

## swing_pullback
- **Inputs**: Polygon daily bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: Trend pullbacks into support with RVOL/dollar volume floors; looks at multi-day trend strength.
- **Alert schema**: Symbol, price, trend stats, pullback depth; Telegram text.
- **False positives**: Choppy sideways names, thin float mean-reversions.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## panic_flush
- **Inputs**: Polygon minute + daily bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: Sharp downside flushes (large negative move vs open) with RVOL spike, distance below VWAP.
- **Alert schema**: Symbol, drop %, RVOL, VWAP distance, recovery hints; Telegram text.
- **False positives**: Halt/reopen whipsaws, single print downticks.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## momentum_reversal
- **Inputs**: Polygon intraday bars + daily context.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: Mean-reversion setups: strong move followed by reclaim/engulf of VWAP or key level; RVOL filter.
- **Alert schema**: Symbol, price, RVOL, reclaim reason, timestamps; Telegram text.
- **False positives**: Low liquidity wicks, news-driven reversals without confirmation.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## trend_rider
- **Inputs**: Polygon daily + intraday bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: Continuation trends with higher-high/higher-low structure and RVOL support.
- **Alert schema**: Symbol, price, trend metrics, RVOL; Telegram text.
- **False positives**: Late-stage exhaustion moves, low-float squeezes.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## rsi_signals
- **Inputs**: Polygon daily bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: RSI oversold/overbought crosses with trend filter and dollar volume floors.
- **Alert schema**: Symbol, RSI value, trend direction, last price; Telegram text.
- **False positives**: Range-bound chop where RSI whipsaws.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## opening_range_breakout (orb)
- **Inputs**: Polygon intraday minute bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH but constrained to opening range window (first N minutes).
- **Triggers**: Breaks above/below defined opening range with RVOL and dollar volume filters.
- **Alert schema**: Symbol, direction, range size, confirmation notes; Telegram text.
- **False positives**: Early liquidity spikes, fake-outs around ORB close.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## options_cheap_flow
- **Inputs**: Polygon options chain and last trades (using price/size `p`/`s` keys when available).
- **Universe**: Options contracts for dynamic equity universe; honors `OPTIONS_FLOW_TICKER_UNIVERSE` override.
- **Window**: RTH.
- **Triggers**: Low-premium contracts with unusual size relative to typical trade size and OI ratio.
- **Alert schema**: Underlying, contract, premium, size, DTE, IV summary; Telegram text.
- **False positives**: Sweep noise on popular tickers, misparsed trades without `p`/`s` keys.
- **Dependencies**: `bots.options_common`, `bots.shared`, `bots.status_report`.

## options_unusual_flow
- **Inputs**: Polygon options chain and last trades.
- **Universe**: Options for dynamic equity universe.
- **Window**: RTH.
- **Triggers**: Large notional or size sweeps/blocks materially above typical sizes with OI/volume filters.
- **Alert schema**: Underlying, contract, side, premium/notional, IV stats; Telegram text.
- **False positives**: ETF hedges, closing trades that look large but match OI.
- **Dependencies**: `bots.options_common`, `bots.shared`, `bots.status_report`.

## options_whales
- **Inputs**: Polygon options chain and last trades.
- **Universe**: Options for dynamic equity universe.
- **Window**: RTH.
- **Triggers**: Very high notional or multi-sweep whale trades with far OTM/ITM filters and DTE bands.
- **Alert schema**: Underlying, whale tag, contract, price/size, notional, DTE; Telegram text.
- **False positives**: Hedging blocks, roll transactions without context.
- **Dependencies**: `bots.options_common`, `bots.shared`, `bots.status_report`.

## options_iv_crush
- **Inputs**: Polygon options chain, implied vol snapshots.
- **Universe**: Options for dynamic equity universe.
- **Window**: RTH.
- **Triggers**: Rapid IV collapses post-event with price stability checks.
- **Alert schema**: Underlying, contract, IV drop %, price context; Telegram text.
- **False positives**: Bad ticks on IV, stale chain data.
- **Dependencies**: `bots.options_common`, `bots.shared`, `bots.status_report`.

## options_indicator
- **Inputs**: Polygon options indicators/greeks endpoints.
- **Universe**: Options for dynamic equity universe.
- **Window**: RTH.
- **Triggers**: Indicator-based signals (IV percentile, skew shifts) above configured thresholds.
- **Alert schema**: Underlying, indicator values, DTE band, IV context; Telegram text.
- **False positives**: Stale greeks, thinly traded strikes.
- **Dependencies**: `bots.options_common`, `bots.shared`, `bots.status_report`.

## squeeze
- **Inputs**: Polygon daily + intraday bars.
- **Universe**: Dynamic equities via `resolve_universe_for_bot`.
- **Window**: RTH.
- **Triggers**: Volatility compression / squeeze breakouts with RVOL confirmation and close vs range checks.
- **Alert schema**: Symbol, squeeze state, breakout direction, RVOL; Telegram text.
- **False positives**: Fake breaks in low volume, news halts.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## dark_pool_radar
- **Inputs**: Dark pool prints feed via Polygon/alt endpoint.
- **Universe**: Dynamic equities.
- **Window**: RTH.
- **Triggers**: Unusually large dark pool prints relative to ADV and price impact.
- **Alert schema**: Symbol, notional/size, VWAP relation, print count; Telegram text.
- **False positives**: Single negotiated crosses that do not impact price.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## earnings
- **Inputs**: Benzinga earnings via `fetch_benzinga_earnings`, Polygon calendars.
- **Universe**: All tickers returned by API.
- **Window**: All-day; throttled to 5m default.
- **Triggers**: Upcoming earnings within window; filters by volume floor.
- **Alert schema**: Company, date/time, period, EPS/Rev expectations; Telegram text.
- **False positives**: Placeholder calendar entries without confirmed times.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## daily_ideas
- **Inputs**: Aggregated stats from other bots / market data (daily bars, RVOL).
- **Universe**: Dynamic equities.
- **Window**: RTH and late-day wrap.
- **Triggers**: Daily trade ideas combining RVOL, trend, squeezes.
- **Alert schema**: Symbols list with rationale; Telegram text.
- **False positives**: Overlap with other signals; broad market moves.
- **Dependencies**: `bots.shared`, `bots.status_report`.

## status_report
- **Inputs**: Stats file maintained by `bots.shared`.
- **Universe**: All bots.
- **Window**: Any time (heartbeat interval env-controlled).
- **Triggers**: Periodic status heartbeat summarizing run counts/errors.
- **Alert schema**: JSON / Telegram summary of bot stats.
- **False positives**: Stale stats file when bots disabled.
- **Dependencies**: `bots.shared` stats helpers.

## equity_flow / intraday_flow / trend_flow / gap_scanner / dark_pool bots (legacy)
- **Inputs**: Polygon intraday & trade feeds.
- **Universe**: Dynamic equities with overrides.
- **Window**: RTH.
- **Triggers**: Equity flow / trend momentum variants; use RVOL, VWAP, and price filters.
- **Alert schema**: Symbol, price, RVOL, trade rationale; Telegram text.
- **False positives**: Overlap with primary flow bots; thin volume.
- **Dependencies**: `bots.shared`, `bots.options_common` (for mixed flows).

