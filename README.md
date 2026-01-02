# MoneySignalAI — Real-Time Multi-Strategy Trading Intelligence

## 1) Executive Summary
MoneySignalAI is a real-time, multi-strategy AI trading intelligence platform that simultaneously scans equities, options, earnings events, and dark pool activity. It is engineered for scale, low latency, and institutional-style signal detection while remaining accessible to advanced retail traders, professional desks, and future SaaS/API consumers.

The platform focuses on actionable alerts rather than raw market data. Each strategy blends deterministic rules with intelligence-style inference to score context, suppress noise, and surface only the most tradeable opportunities. A shared intelligence layer allows dozens of bots to run concurrently while reusing market context (price, volume, options flow, and events) so alerts are consistent across strategies.

Outputs are delivered as concise, trader-ready signals (bullish, bearish, or neutral) that can be consumed by humans or downstream systems. Configuration is environment-driven, enabling institutional-style knobs for risk, liquidity, and sensitivity across equities and derivatives.

## 2) System Architecture (High-Level)
- **Modular bot-based architecture**: Each strategy is isolated in its own module with an async `run_*` entrypoint, keeping logic independent and fault boundaries clear.
- **Central async scheduler**: `main.py` orchestrates bots on configurable intervals, enforces time windows (premarket, RTH, opening-range), and applies per-bot timeouts.
- **Shared data caching**: Common helpers (in `bots/shared.py` and `bots/options_common.py`) manage EST-aware time logic, dynamic ticker universes, and cached Polygon.io snapshots for prices and options chains to reduce API pressure.
- **Polygon.io market data ingestion**: Equities and options data are ingested via Polygon endpoints with layered fallbacks and caching.
- **Real-time alert pipeline**: Bots emit structured alerts that are formatted for downstream delivery (e.g., Telegram today, extensible to webhooks or SaaS endpoints).
- **Status + heartbeat monitoring**: `bots/status_report.py` aggregates run stats, errors, and latency to publish a heartbeat for operational transparency.

**Flow (simplified)**
```
Market Data (Polygon.io) → Strategy Bots → Signal Scoring → Alert Engine → Distribution
```

Bots run concurrently, remain isolated by strategy, share market context, and never block each other thanks to semaphore-based concurrency control and per-bot timeouts.

## 3) AI & Intelligence Layer
This is market-intelligence AI, not conversational AI. The system performs:
- **Pattern recognition**: Detects structural behaviors such as breakouts, squeezes, capitulation, and IV crushes.
- **Context-aware filtering**: Combines price, volume, VWAP, open interest, implied volatility, delta, and time-in-session to filter noise.
- **Multi-signal confirmation**: Weak signals (e.g., modest RVOL plus VWAP reclaim plus options flow) are combined to raise confidence.
- **Noise suppression**: Dynamic liquidity floors, RVOL gates, and duplicate-signal suppression prevent alert spam.
- **Behavioral inference**: Reads volume/price/volatility relationships and options flow to infer whether institutions are leaning long or short.

Each bot mixes deterministic thresholds with intelligence-style weighting. Signals are scored to avoid over-alerting; for example, a breakout may be suppressed if RVOL is weak or if price is extended far above VWAP. The architecture is designed to evolve toward ML-assisted weighting and cross-bot consensus scoring without changing the runtime model.

## 4) Bot Overview (High Level)
- **Equity Momentum & Structure Bots**: Premarket, Volume Monster, Gap Flow, Swing Pullback, Trend Rider, Panic Flush, Momentum Reversal, RSI Signals, ORB (Opening Range Breakout), Squeeze.
- **Options Flow & Derivatives Bots**: Options Cheap Flow, Options Unusual Flow, Options Whales, Options IV Crush, Options Indicator.
- **Event-Based Bots**: Earnings, Dark Pool.
- **Idea Generation & Meta Bots**: Daily Ideas (cross-signal confluence for curated long/short lists).

## 5) Detailed Bot Descriptions
Alert formats below mirror how the platform communicates with traders and downstream systems. Timestamps are EST. Prices are illustrative but reflect the logic implemented in the codebase.

### Equity Bots

**Premarket**  
- **Behavior**: Identifies premarket gappers with meaningful liquidity and RVOL.  
- **How it works**: Scans a premarket window only; filters by minimum price, premarket % move, dollar volume, and RVOL; compares to prior close and intraday range.  
- **Alert Types**: Bullish gap-up, Bearish gap-down.  
- **Example Alerts**:
```text
PREMARKET — NVDA | Bullish Gap
Time: 08:57 AM EST
Price: $482.30 (+3.9% vs prior close)
Premarket Range: $476.10 – $485.90 | Vol: 950,000 (≈$458M) | RVOL 2.1x
Why: Liquidity + RVOL + gap > MIN_PREMARKET_MOVE_PCT; holding upper half of range.
Use Case: Opening-drive setup or gap-and-go validation.
```
```text
PREMARKET — NKE | Bearish Gap
Time: 09:05 AM EST
Price: $96.20 (-4.4% vs prior close)
Range: $95.80 – $97.40 | Vol: 410,000 (≈$39M) | RVOL 1.7x
Why: Gap-down with solid liquidity; near lows of premarket range.
Use Case: Watch for gap-fill short or continuation.
```
- **Typical Use Case**: Opening-drive trades, early bias framing, liquidity validation before the bell.

**Volume Monster**  
- **Behavior**: Spots abrupt liquidity surges that imply institutional participation.  
- **How it works**: During RTH, checks dollar volume, RVOL, and velocity versus recent baseline; ignores thin names via global floors.  
- **Alert Types**: Bullish or Bearish depending on price direction.  
- **Example Alerts**:
```text
VOLUME MONSTER — AMZN | Bullish Surge
Time: 02:18 PM EST
Price: $154.80 (+2.6% today)
Volume: 12.4M | RVOL 3.4x | Dollar Vol: $1.9B
Why: Volume spike above VOLUME_MONSTER_RVOL with price holding above VWAP.
Use Case: Momentum confirmation or add-on entry.
```
```text
VOLUME MONSTER — COIN | Bearish Surge
Time: 01:05 PM EST
Price: $92.10 (-5.1% today)
Volume: 8.8M | RVOL 2.9x | Dollar Vol: $810M
Why: High RVOL selling as price loses VWAP.
Use Case: Trend continuation short; risk recalibration for longs.
```
- **Typical Use Case**: Confirm strength/weakness, gauge institutional interest.

**Gap Flow**  
- **Behavior**: Tracks whether premarket gaps hold or fade after the open.  
- **How it works**: Requires prior gap + liquidity; during RTH checks if price sustains above/below gap levels with supportive RVOL and minimal fade.  
- **Alert Types**: Bullish hold, Bearish fade.  
- **Example Alerts**:
```text
GAP FLOW — SHOP | Bullish Hold
Time: 10:02 AM EST
Price: $78.90 (+6.1% from prior close)
Context: Opened above gap; holding +3.2% above VWAP with RVOL 2.0x.
Why: Gap persisting with support; minimal fade through first 30 minutes.
Use Case: Continuation trades; avoid fighting strong gap.
```
```text
GAP FLOW — INTC | Bearish Fade
Time: 09:58 AM EST
Price: $40.25 (+0.8% from prior close)
Context: Gapped +4.0% but failed; now -2.9% from open, under VWAP; RVOL 1.6x.
Why: Gap sold off; momentum shifted negative.
Use Case: Gap-fill short or avoid long bias.
```
- **Typical Use Case**: Determine whether to trade with or against a gap after liquidity confirms direction.

**Swing Pullback**  
- **Behavior**: Highlights controlled pullbacks inside strong uptrends.  
- **How it works**: Looks for multi-day uptrends (stacked MAs, higher highs) with current-day dip to key moving averages or VWAP, supported by RVOL and limited damage from highs.  
- **Alert Types**: Bullish only (controlled pullback for potential bounce).  
- **Example Alert**:
```text
SWING PULLBACK — LLY | Bullish
Time: 12:40 PM EST
Price: $705.50 (-1.8% today)
Trend: 20D uptrend; price retesting 21EMA; RVOL 1.3x
Why: Pullback within strong structure; within SWING_PULLBACK_MAX_DROP% and above higher-timeframe MA stack.
Use Case: Staged swing entries with defined risk near support.
```
- **Typical Use Case**: Add-on entries in established trends; swing positioning.

**Trend Rider**  
- **Behavior**: Detects clean trend continuation and breakouts with aligned structure.  
- **How it works**: Requires stacked moving averages, VWAP alignment, RVOL, and breakout distance vs prior highs. Screens out extended or illiquid names.  
- **Alert Types**: Bullish continuation or Bearish trend breakdown.  
- **Example Alerts**:
```text
TREND RIDER — MSFT | Bullish Continuation
Time: 11:22 AM EST
Price: $348.10 (+1.9% today)
Structure: Above 8/21/50 EMA stack; VWAP support; RVOL 1.5x
Why: Trend strength with fresh intraday breakout > TREND_RIDER_MIN_BREAKOUT_PCT.
Use Case: Momentum continuation; ride institutional trend.
```
```text
TREND RIDER — IWM | Bearish Breakdown
Time: 02:05 PM EST
Price: $182.30 (-2.4% today)
Structure: Below 8/21/50 EMA stack; VWAP resistance; RVOL 1.7x
Why: Multi-day trend loss with RVOL; confirms downside trend shift.
Use Case: Short confirmation; hedge selection.
```
- **Typical Use Case**: Momentum following, risk-managed continuation plays.

**Panic Flush**  
- **Behavior**: Flags capitulation-style selling near intraday lows.  
- **How it works**: Looks for large % drops, high RVOL, and price pinned near session lows with minimal bounces. Avoids reversal confirmation; focuses on stress identification.  
- **Alert Types**: Bearish only.  
- **Example Alert**:
```text
PANIC FLUSH — AFRM | Bearish
Time: 01:48 PM EST
Price: $29.40 (-11.6% today)
Context: RVOL 3.1x; trading within 1.2% of LOD; heavy sell pressure.
Why: Meets PANIC_FLUSH_MIN_DROP and RVOL; stuck near lows.
Use Case: Risk warning for longs; potential exhaustion watch for contrarians.
```
- **Typical Use Case**: Risk management, caution on knife-catching, alerting for potential exhaustion setups.

**Momentum Reversal**  
- **Behavior**: Detects intraday reversals after extended moves.  
- **How it works**: Checks for prior extreme move, VWAP reclaim/loss, percentage snapback from extremes, and RVOL confirmation.  
- **Alert Types**: Bullish reversal after selloff; Bearish reversal after squeeze.  
- **Example Alerts**:
```text
MOMENTUM REVERSAL — META | Bullish Reclaim
Time: 02:30 PM EST
Price: $327.50 (-3.2% from highs)
Context: Morning selloff -5.8%; now reclaimed VWAP with RVOL 1.8x; +2.6% off lows.
Why: VWAP reclaim + range recovery within MOMO thresholds.
Use Case: Intraday reversal entry; stop below VWAP.
```
```text
MOMENTUM REVERSAL — TSLA | Bearish Failure
Time: 11:15 AM EST
Price: $232.80 (+4.9% today)
Context: Early squeeze +7.5%; lost VWAP; now -2.5% off highs with RVOL 2.2x.
Why: Exhaustion plus VWAP rejection triggers bearish reversal criteria.
Use Case: Short against VWAP after failed squeeze.
```
- **Typical Use Case**: Intraday reversal trades with defined risk at VWAP/levels.

**RSI Signals**  
- **Behavior**: Surfaces high-conviction RSI extremes on liquid names.  
- **How it works**: Applies RSI bands with dollar-volume and price floors; avoids thin names.  
- **Alert Types**: RSI Overbought (bearish skew) or Oversold (bullish skew).  
- **Example Alerts**:
```text
RSI SIGNAL — COST | Oversold
Time: 01:00 PM EST
Price: $502.10 (-2.1% today)
RSI(14): 27 | RVOL 1.4x | Dollar Vol: $1.2B
Why: RSI below RSI_OVERSOLD with liquidity confirmation.
Use Case: Mean-reversion scout; pair with level-based entries.
```
```text
RSI SIGNAL — NVDA | Overbought
Time: 12:18 PM EST
Price: $496.80 (+3.6% today)
RSI(14): 78 | RVOL 1.9x | Dollar Vol: $4.8B
Why: RSI above RSI_OVERBOUGHT with strong volume.
Use Case: De-risk longs; lookout for pullback or hedge.
```
- **Typical Use Case**: Mean reversion signals, hedge timing, confirmation for other bots.

**ORB (Opening Range Breakout)**  
- **Behavior**: Tracks breaks of the opening range with volume validation.  
- **How it works**: Monitors a configurable opening range window; requires RVOL and VWAP context before confirming breakout above/below the range.  
- **Alert Types**: Bullish breakout or Bearish breakdown.  
- **Example Alerts**:
```text
ORB — NFLX | Bullish Breakout
Time: 09:53 AM EST
Price: $458.40 (+1.9% since open)
Opening Range: $450.20 – $453.10 (15 min)
Why: Cleared opening range with RVOL 1.7x; holding above VWAP.
Use Case: Opening drive continuation with defined range risk.
```
```text
ORB — BA | Bearish Breakdown
Time: 09:48 AM EST
Price: $198.10 (-1.6% since open)
Opening Range: $199.90 – $203.20 (15 min)
Why: Broke below range with RVOL 1.6x; VWAP overhead.
Use Case: Early trend alignment; avoid long attempts.
```
- **Typical Use Case**: Early session bias, scalp-to-swing transitions with tight risk.

**Squeeze**  
- **Behavior**: Detects volatility compression that sets up expansion.  
- **How it works**: Looks for Bollinger/Keltner-style compression, declining range, and alignment with trend indicators; confirms expansion with volume.  
- **Alert Types**: Bullish expansion or Bearish breakdown from squeeze.  
- **Example Alerts**:
```text
SQUEEZE — MU | Bullish Expansion
Time: 01:22 PM EST
Price: $88.70 (+1.4% today)
Context: Multi-hour range compression; BB inside Keltner; RVOL 1.5x on breakout.
Why: Expansion trigger after confirmed squeeze; price above VWAP and MA stack.
Use Case: Range-break momentum with clear invalidation.
```
```text
SQUEEZE — SNAP | Bearish Expansion
Time: 02:10 PM EST
Price: $10.45 (-3.1% today)
Context: Tight compression all morning; breakdown with RVOL 1.8x; below VWAP.
Use Case: Short continuation out of squeeze; scalp or day trade.
```
- **Typical Use Case**: Breakout/breakdown trades after identified compression.

### Options Bots

**Options Cheap Flow**  
- **Behavior**: Surfaces inexpensive contracts with meaningful size/notional.  
- **How it works**: Filters for low premium under a cap, minimum size and notional, and adequate underlying price; tags context like DTE and relative OI.  
- **Alert Types**: Bullish or Bearish depending on call/put direction.  
- **Example Alerts**:
```text
OPTIONS CHEAP FLOW — AMD | Bullish Calls
Time: 01:05 PM EST
Contract: 2,500x 02/16/2026 $140C | Premium $0.38 | Notional $95,000 | 18 DTE
Context: Low-cost lotto-sized call sweep; underlying $132.10; near-term catalyst.
Use Case: Speculative momentum participation with defined premium risk.
```
```text
OPTIONS CHEAP FLOW — X | Bearish Puts
Time: 11:42 AM EST
Contract: 1,800x 03/15/2026 $25P | Premium $0.42 | Notional $75,600 | 35 DTE
Context: Cheap downside protection with size; underlying $27.05.
Use Case: Hedge indicator or short confirmation.
```
- **Typical Use Case**: Identify speculative positioning or inexpensive hedges.

**Options Unusual Flow**  
- **Behavior**: Flags outlier option orders relative to normal flow.  
- **How it works**: Requires size/notional above thresholds, bounded DTE, and liquidity floors; compares to OI/volume ratios to judge anomaly.  
- **Alert Types**: Bullish (calls/bullish spreads) or Bearish (puts/bearish spreads).  
- **Example Alerts**:
```text
OPTIONS UNUSUAL — MS | Bullish Calls
Time: 10:55 AM EST
Contract: 3,200x 04/18/2026 $95C | Premium $2.40 | Notional $768,000 | 58 DTE
Context: Size 2.6x daily average; OI 1,050; RVOL in equity 1.3x.
Use Case: Detect institutional leaning; confirm equity strength.
```
```text
OPTIONS UNUSUAL — DAL | Bearish Puts
Time: 12:30 PM EST
Contract: 4,100x 03/01/2026 $38P | Premium $1.05 | Notional $430,500 | 24 DTE
Context: Size materially above OI; equity RVOL 1.5x to downside.
Use Case: Downside confirmation or hedge insight.
```
- **Typical Use Case**: Institutional footprint detection, direction confirmation.

**Options Whales**  
- **Behavior**: Captures very large notional trades indicative of whale participation.  
- **How it works**: Enforces high notional and size thresholds with short-to-medium DTE filters; validates underlying liquidity and price floors.  
- **Alert Types**: Bullish or Bearish depending on side.  
- **Example Alerts**:
```text
OPTIONS WHALE — AAPL | Bullish Calls
Time: 02:02 PM EST
Contract: 10,000x 03/20/2026 $200C | Premium $3.25 | Notional $3,250,000 | 60 DTE
Context: Whale-size sweep; underlying $191.40; equity RVOL 1.4x.
Use Case: High-conviction follow or sentiment gauge.
```
```text
OPTIONS WHALE — JPM | Bearish Puts
Time: 01:18 PM EST
Contract: 6,500x 03/15/2026 $150P | Premium $2.80 | Notional $1,820,000 | 45 DTE
Context: Large downside bet; OI 2,900; IV elevated.
Use Case: Downside confirmation, hedge alignment for financials.
```
- **Typical Use Case**: Track large directional commitments, inform hedging or follow-on trades.

**Options IV Crush**  
- **Behavior**: Detects sharp implied volatility drops post-catalyst.  
- **How it works**: Compares IV before/after; requires minimum IV drop %, contract volume, and bounded DTE; contextualizes with underlying move.  
- **Alert Types**: Neutral/bias depends on price action (often post-event).  
- **Example Alert**:
```text
IV CRUSH — NFLX
Time: 09:45 AM EST
Underlying: $398.10 (-6.0% today)
Contract: 1,200x 02/14/2026 $420C | IV: 144% → 92% (-52%) | Premium $3.10 | 24 DTE
Why: Post-earnings IV collapse beyond IVCRUSH_MIN_IV_DROP_PCT with real volume.
Use Case: Reprice expectations; evaluate selling premium or avoiding stale volatility.
```
- **Typical Use Case**: Spot premium collapse; inform vol-selling or avoid overpaying for options.

**Options Indicator**  
- **Behavior**: Provides regime-style analytics on IV momentum vs reversal with technical overlays.  
- **How it works**: Computes IV ranks, RSI, MACD, Bollinger context, and OI/volume stats; classifies regimes (e.g., high-IV momentum, low-IV reversal) and biases direction accordingly.  
- **Alert Types**: Bullish, Bearish, or Neutral bias depending on regime.  
- **Example Alerts**:
```text
OPTIONS INDICATOR — SPY | High-IV Momentum (Bullish)
Time: 02:15 PM EST
Underlying: $479.60 (+1.7% today) | RVOL 1.3x
IV Rank: 82 | RSI(14): 65 | MACD: +0.14 vs Signal +0.09 | Bollinger: near upper band
Why: High-IV uptrend with supportive momentum indicators.
Use Case: Align delta exposure with vol regime; bias long.
```
```text
OPTIONS INDICATOR — QQQ | Low-IV Reversal (Bearish Bias)
Time: 11:50 AM EST
Underlying: $382.40 (-0.8% today)
IV Rank: 24 | RSI(14): 74 | MACD: negative cross | Bollinger: tagging upper band
Why: Low-IV stretch with momentum fading; sets up reversal risk.
Use Case: Hedge timing; consider mean-reversion shorts.
```
- **Typical Use Case**: Regime-aware risk framing for options traders and portfolio overlays.

### Event / Meta Bots

**Earnings**
- **Behavior**: Highlights notable earnings movers and upcoming catalysts.
- **How it works**: Scans scheduled earnings within a forward window; during/after events, checks price move, dollar volume, and gap behavior to decide if alert-worthy.
- **Alert Types**: Bullish beat/strength, Bearish miss/weakness.
- **Key Env Vars**:
  - `MASSIVE_BASE_URL` (default: Polygon host) to point at the Massive Benzinga API base.
  - `MIN_EARNINGS_PRICE`, `MIN_EARNINGS_MOVE_PCT`, `MIN_EARNINGS_DOLLAR_VOL` to gate price/volume quality.
  - `EARNINGS_MIN_IMPORTANCE`, `EARNINGS_ALLOWED_DATE_STATUSES` to filter Benzinga events (e.g., confirmed/projected).
  - `EARNINGS_EVENT_MAX_AGE_HOURS` to ignore stale reports; `EARNINGS_PREMARKET_WINDOW`, `EARNINGS_AFTERHOURS_WINDOW`, `EARNINGS_FOLLOWTHROUGH_WINDOW` to gate alert windows.
  - `EARNINGS_POS_SURPRISE_PCT`, `EARNINGS_NEG_SURPRISE_PCT` to score beats/misses on EPS/revenue surprise percentages.
- **Example Alert**:
```text
EARNINGS — LULU | Post-Print Strength
Time: 09:12 AM EST
Price: $408.50 (+7.8% premarket)
Context: Beat EPS/rev; RVOL 3.0x; holding top of premarket range.
Use Case: Event-driven momentum watch; informs ORB/gap strategies.
```
- **Typical Use Case**: Catalyst tracking for intraday or swing setups around earnings windows.

**Dark Pool**  
- **Behavior**: Surfaces unusual dark-pool prints by count, notional, and largest block.  
- **How it works**: Aggregates today’s dark-pool activity; applies thresholds on total notional and largest print size over a lookback window.  
- **Alert Types**: Neutral directional bias; highlights accumulation or distribution context.  
- **Example Alert**:
```text
DARK POOL RADAR — AMD
Time: 03:05 PM EST
Largest Print: $38.5M | Total DP Notional: $142M across 126 prints | Lookback: 45m
Why: Exceeds DARK_POOL_MIN_NOTIONAL and largest-print threshold.
Use Case: Track stealth accumulation/distribution; pair with price trend.
```
- **Typical Use Case**: Flow awareness to complement momentum or reversal signals.

**Daily Ideas**  
- **Behavior**: Curates long/short ideas twice daily using cross-bot confluence.  
- **How it works**: Scores trend, VWAP posture, RVOL, RSI, and options bias to rank top candidates; posts AM and PM lists.  
- **Alert Types**: Long ideas and Short ideas lists.  
- **Example Alert**:
```text
DAILY IDEAS — AM Session
Top Longs: NVDA, LLY, PANW (trend + RVOL + bullish options bias)
Top Shorts: COIN, RBLX, SNAP (weak structure + RVOL + bearish flow)
Context: Generated 10:52–11:00 AM slot from cross-signal scoring.
Use Case: Focus list for day traders and PM desk recaps.
```
- **Typical Use Case**: Idea curation; align desk focus on highest-quality setups.

## 6) Scheduler & Concurrency Model
- **Async orchestration**: `main.py` runs an asyncio loop with configurable base scan interval (`SCAN_INTERVAL_SECONDS`).
- **Per-bot intervals**: Each bot has its own cadence, overridable via env (e.g., `premarket_interval`).
- **Non-blocking execution**: Bots run inside an asyncio semaphore (`BOT_MAX_CONCURRENCY`) so long scans cannot starve faster ones.
- **Timeout enforcement**: `BOT_TIMEOUT_SECONDS` guards against hung calls; errors are captured and recorded without stopping the scheduler.
- **Time-window gating**: Premarket-only bots, RTH-only bots, and ORB windows are enforced centrally and optionally relaxed via env flags.
- **Rate-limit safety**: Shared caching of universes, prices, and options chains reduces redundant Polygon calls; retries/backoff are localized in shared helpers.
- **Scalability**: Adding strategies only registers a new bot tuple—no scheduler rewrite—so dozens of strategies can run concurrently while sharing the same market context cache.

## 7) Reliability, Monitoring & Heartbeat
- **Heartbeat**: `bots/status_report.py` emits a heartbeat summarizing last-run times, scan counts, matches, and alerts per bot.
- **Runtime metrics**: Median and last runtime per bot highlight latency or degradation.
- **Health detection**: Flags bots with high scans but zero alerts, zero scans, or no runs today. Errors are recorded with context.
- **Failure isolation**: Per-bot exceptions are contained; failed runs still register for observability.
- **Why it matters**: Traders and integrators gain trust that signals are timely, and operators can quickly triage disabled or underperforming strategies.

## 8) Data Sources & Integrations
- **Polygon.io**: Primary provider for equities (prices, volume, VWAP) and options (chains, last trades).
- **Shared caching**: Universe resolution and option chain retrieval are cached with TTLs to minimize API load and latency.
- **Extensible feeds**: Architecture allows substituting or augmenting Polygon with additional providers without altering bot contracts.

## 9) Configuration & Extensibility
- **Environment-based configuration**: Every threshold (RVOL, price floors, gap %, IV drop, DTE caps, etc.) is set via environment variables for per-deployment tuning.
- **Adding new bots**: Implement an async `run_*` in `bots/<strategy>.py`, register in `main.py`, and optionally add a `should_run_now` gate; shared helpers provide universes, time windows, and alert formatting.
- **Strategy experimentation**: TEST_MODE and DISABLED_BOTS allow selective activation; per-bot intervals and time windows make it safe to iterate in production.
- **Future SaaS/API model**: Alert payloads are already structured for multi-channel delivery; config can be externalized for customer-specific routing and thresholds.

## 10) Vision & Roadmap (Investor-Oriented)
- **Scale-first architecture**: Modular bots, cached data plane, and async scheduling support more strategies without architectural change.
- **Path to SaaS**: The alert pipeline and status heartbeat can back a managed signals product, web dashboard, or API/white-label offering with tenant-specific configs.
- **Machine-learning evolution**: Current rule+context framework provides labeled outcomes (scan → match → alert) that can feed ML models for weighting, cross-bot consensus, and quality tiers.
- **Institutional readiness**: Liquidity-aware filters, rate-limit protections, and fault isolation align with institutional expectations for reliability and auditability.

MoneySignalAI delivers actionable, low-latency trading intelligence by fusing deterministic strategy logic with an evolving AI-style inference layer—built for traders today and extensible to institutional-grade SaaS tomorrow.

## New runtime safeguards
- `UNIVERSE_HARD_CAP` (default 800): global maximum tickers any bot will scan; enforced with diagnostics when trimmed.
- `BOT_MAX_RUNTIME_SECONDS` (default equals `BOT_TIMEOUT_SECONDS`): hard per-run ceiling applied by the scheduler.
- `BOT_MAX_CONCURRENCY` (default 6, capped to 2 when `INTEGRATION_TEST=true`): limits simultaneous bot executions.
- `MAX_REQUESTS_PER_BOT_PER_RUN` (default 400): stops runaway HTTP loops per bot run.
- `CIRCUIT_BREAKER_FAILURES` / `CIRCUIT_BREAKER_COOLDOWN`: open a cooldown after repeated HTTP failures.
- `TEST_MODE=true`: runs bots against local fixtures without network usage.
- `INTEGRATION_TEST=true`: constrains universes and concurrency for safe limited-network verification.
