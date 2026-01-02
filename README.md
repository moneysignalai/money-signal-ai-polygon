# MoneySignalAI — Stock & Options Data Engine

MoneySignalAI is an institutional-grade, multi-bot equities and options alerting platform built in Python. It scans a dynamic top-volume universe (up to ~1,500 tickers with `TICKER_UNIVERSE` fallback), applies strategy-specific filters, and streams emoji-rich, trader-ready alerts to Telegram. The FastAPI scheduler runs multiple bots in parallel, enforces time windows, and emits a heartbeat with per-bot health, scan counts, and runtimes.

- **AI-powered, modular bot engine** across equity momentum, intraday flows, gaps, squeezes, dark pool, earnings, options flow, analytics, and daily ideas.
- **Real-time Polygon/Massive data** with EST-aware trading-day logic and dynamic top-volume universes.
- **Env-driven tuning** for every threshold (RVOL, dollar volume, IV crush %, DTE, gap %, RSI bands, etc.).
- **Production telemetry** via `status_report.py` (today-only stats, diagnostics, runtimes) and Telegram delivery.
- **Scales to 1,500+ tickers** with safe fallbacks and per-bot timeouts.

---

## 1️⃣ System Architecture Overview
- **Scheduler / FastAPI (`main.py`)**
  - Registry of bots (public name, module, async entrypoint, interval, schedule type).
  - Applies `DISABLED_BOTS`, `TEST_MODE_BOTS`, RTH/premarket/slot gates, and per-bot timeouts.
  - Runs bots concurrently with asyncio and captures per-bot errors via `record_error`.
- **Shared utilities (`bots/shared.py`)**
  - EST time helpers (`format_est_timestamp`, `now_est`, RTH/premarket checks, trading-day detection).
  - Dynamic universe resolver (top-by-volume, capped at 1,500; fallback `TICKER_UNIVERSE`).
  - Data helpers (RVOL, RSI, MAs, Bollinger, VWAP), Telegram senders (`send_alert`, `send_alert_text`), chart links, stats helpers (`record_bot_stats`).
- **Options utilities (`bots/options_common.py`)**
  - OCC parsing, contract display, IV/notional/DTE helpers, and premium formatters for all option flow bots.
- **Bots (`bots/*.py`)**
  - Each strategy exposes an async `run_*` entrypoint, reads env thresholds once, uses shared helpers for universes/time windows, and always records stats.
- **Status / Heartbeat (`bots/status_report.py`)**
  - Loads `STATUS_STATS_PATH`, aggregates today-only scanned/matches/alerts, diagnostics (high scan/zero alerts, zero scans, not run today), runtimes, and sends the MoneySignalAI Heartbeat to Telegram.

**Text diagram**
```
[Polygon/Massive API] -> shared.py (time, universe, data, alerts, stats)
                      -> options_common.py (option parsing/formatting)
main.py scheduler -> bot registry -> async run_* per bot -> record_bot_stats
status_report.py -> today-only aggregation -> heartbeat to Telegram
Telegram -> alerts + heartbeat delivered to TELEGRAM_CHAT_ALL
```

---

## 2️⃣ Full Bot List + What They Do
Each bot uses the shared dynamic universe (top-by-volume capped at ~1,500) with `TICKER_UNIVERSE` fallback and EST time gating unless noted.

- **Premarket Scanner** – Finds premarket gappers with RVOL/price/dollar-vol floors. Env: `MIN_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, `MIN_PREMARKET_PRICE`, `PREMARKET_TICKER_UNIVERSE`. Runs premarket window only.
- **Volume Monster** – Pure liquidity explosion detector (institutional participation). Env: `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `VOLUME_MONSTER_RVOL`, global floors. Runs RTH.
- **Gap Flow** – Gap + continuation behavior (holding strength after open). Env: `MIN_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, global floors. Runs RTH.
- **Trend Rider** – Institutional trend continuation & breakout structure (stacked MAs, VWAP alignment). Env: `TREND_RIDER_MIN_DOLLAR_VOL`, `TREND_RIDER_MIN_RVOL`, `TREND_RIDER_TREND_DAYS`, `TREND_RIDER_MIN_BREAKOUT_PCT`, global floors. Runs RTH.
- **Swing Pullback** – Controlled dip-buys inside strong uptrends near key MAs. Env: `SWING_*` thresholds (pullback %, trend days, RVOL, dollar vol), global floors. Runs RTH.
- **Panic Flush** – Capitulation detector: heavy down days pinned near lows with big RVOL (not a reversal confirmer). Env: `PANIC_FLUSH_MIN_DROP`, `PANIC_FLUSH_MIN_RVOL`, `PANIC_FLUSH_MAX_FROM_LOW_PCT`, global floors. Runs RTH.
- **Momentum Reversal** – Confirmed intraday reversals after strong moves (VWAP reclaim/loss + range recoveries). Env: `MOMO_REV_MIN_RECLAIM_PCT`, `MOMO_REV_MIN_RVOL`, `MOMO_REV_MIN_MOVE_PCT`, `MOMO_REV_MAX_FROM_EXTREME_PCT`, global floors. Runs RTH.
- **RSI Signals** – Pure RSI extremes (overbought/oversold) with liquidity filters. Env: `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `RSI_MIN_PRICE`, `RSI_MIN_DOLLAR_VOL`, `RSI_MAX_UNIVERSE`, global floors. Runs RTH.
- **Opening Range Breakout (ORB)** – Breaks above/below opening range with volume confirmation and VWAP context. Env: `ORB_RANGE_MINUTES`, `ORB_MIN_DOLLAR_VOL`, `ORB_MIN_RVOL`, `ORB_START_MINUTE`, `ORB_END_MINUTE`, global floors. Runs RTH opening window.
- **Squeeze Bot** – Volatility compression → expansion breakout detector (compression first, then direction). Env: `SQUEEZE_*` thresholds, global floors. Runs RTH.
- **Dark Pool Radar** – Highlights unusual dark-pool prints (count, total notional, largest print) for today. Env: `DARK_POOL_MIN_NOTIONAL`, `DARK_POOL_MIN_LARGEST_PRINT`, `DARK_POOL_LOOKBACK_MINUTES`, global floors. Runs RTH.
- **Earnings Scanner** – Surfaces notable earnings movers/upcoming events. Env: `EARNINGS_MAX_FORWARD_DAYS`, plus earnings price/move/dollar-vol floors. Runs on a slower cadence.
- **Options Cheap Flow** – Low-premium contracts with meaningful size/notional. Env: `CHEAP_MAX_PREMIUM`, `CHEAP_MIN_NOTIONAL`, `CHEAP_MIN_SIZE`, `OPTIONS_MIN_UNDERLYING_PRICE`, `OPTIONS_FLOW_MAX_UNIVERSE`. Runs RTH.
- **Options Unusual Flow** – Outlier size/notional vs typical flow. Env: `UNUSUAL_MIN_NOTIONAL`, `UNUSUAL_MIN_SIZE`, `UNUSUAL_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`. Runs RTH.
- **Options Whale Flow** – Very large “whale” orders. Env: `WHALES_MIN_NOTIONAL`, `WHALES_MIN_SIZE`, `WHALES_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`. Runs RTH.
- **Options IV Crush** – Contracts with sharp IV drops (post-catalyst). Env: `IVCRUSH_MIN_IV_DROP_PCT`, `IVCRUSH_MIN_VOL`, `IVCRUSH_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`. Runs RTH.
- **Options Indicator (Analytics)** – Regime-based IV momentum vs reversal with MACD/RSI/Bollinger/OI context. Env: shared options thresholds + indicator IV rank bounds. Runs RTH.
- **Daily Ideas Bot** – Twice-daily confluence scoring (trend + VWAP + RVOL + RSI + options bias) with top LONG/SHORT lists. Slots: AM (10:45–11:00 ET), PM (15:15–15:30 ET). Uses shared thresholds/universe.

---

## 3️⃣ 📢 Example Alerts
Real template examples mirroring current code output. Timestamps are EST, date format `MM-DD-YYYY`.

### Premarket Scanner
```
📣 PREMARKET — MDB
🕒 09:05 AM EST · Jan 01
💰 $382.40 · 📊 RVOL 1.8x
────────────
🚀 Premarket move: +4.7% vs prior close
📈 Prev Close: $365.10 → Premarket Last: $382.40
📊 Premarket Range: $378.00 – $386.20
📦 Premarket Vol: 1,120,000 (≈ $428,000,000)
🧠 Read: Early momentum with solid liquidity; watch for gap-and-go.
🔗 Chart: https://www.tradingview.com/chart/?symbol=MDB
```

### Volume Monster
```
🧠 VOLUME MONSTER — AXSM
🕒 01-01-2026 · 02:53 PM EST

💰 Price + Move
• Last: $182.64 (+22.8% UP)
• Day Change vs Prev Close: +22.8%
• O $158.49 · H $184.40 · L $158.49 · C $182.64

📊 Liquidity Snapshot
• Volume: 3,059,410
• RVOL: 6.3×
• Dollar Vol: $558,770,642

🧠 Read
Extreme participation vs normal. This is where big money is very active right now.

🔗 Chart
https://www.tradingview.com/chart/?symbol=AXSM
```

### Gap Flow (Gap Up / Gap Down)
```
🧠 GAP FLOW — AXSM
🕒 01-01-2026 · 09:58 AM EST

💰 Gap Stats
• Gap: +6.5% vs prior close (Gap Up 🔼)
• Day Move: +6.5% vs prior close
• O $158.49 · H $184.40 · L $158.49 · Last $182.64

📊 Liquidity
• Volume: 3,059,410
• RVOL: 6.3×
• Dollar Vol: $484,885,891

📈 Continuation Context
• Holding above VWAP: YES
• Holding >60% of gap range: YES
• Direction: Bullish continuation gap

🧠 Read
Strong gap-and-go behavior with real volume behind the move.

🔗 Chart
https://www.tradingview.com/chart/?symbol=AXSM
```
(Gap Down swaps 🔻 and downside continuation text.)

### Trend Rider
```
🚀 TREND RIDER — NVDA
🕒 02:15 PM EST · 01-01-2026

💰 Price + Move
• Last: $522.88 (+4.2% UP)
• O $500.10 · H $525.40 · L $497.50 · C $522.88
• RVOL: 2.1× · Dollar Vol: $8,200,000,000

📈 Trend Structure
• Above 50-day MA: YES
• Above 200-day MA: YES
• Breakout vs 20-day high: $510.20
• Intraday vs VWAP: ABOVE

🧠 Read
Clean, high-volume trend continuation with stacked MAs and fresh breakout.

🔗 Chart
https://www.tradingview.com/chart/?symbol=NVDA
```

### Swing Pullback
```
🎯 SWING PULLBACK — LULU
🕒 01-01-2026 · 11:40 AM EST

💰 Price Snapshot
• Last: $420.15 (-4.1% from recent high)
• O $432.80 · H $435.20 · L $418.10 · C $420.15
• RVOL: 1.1×

📈 Trend Context
• Above 200-day MA: YES
• Above / Near 50-day MA: NEAR (testing support)
• Recent 20-day high: $438.50

🧠 Read
Strong longer-term uptrend with a controlled pullback into support — potential swing-long “buy-the-dip” zone.

🔗 Chart
https://www.tradingview.com/chart/?symbol=LULU
```

### Panic Flush
```
⚠️ PANIC FLUSH — AAPL
🕒 01-01-2026 · 01:45 PM EST

💰 Price + Damage
• Last: $182.10 (-4.8% today)
• O $194.00 · H $195.10 · L $180.55 · C $182.10
• Distance from LOD: 0.9%

📊 Liquidity
• Volume: 78,200,000
• RVOL: 3.4×
• Dollar Vol: $14,200,000,000

📉 Context
• VWAP: BELOW
• RSI(14): 31.2 (pressure zone)

🧠 Read
Heavy capitulation selling with price pinned near lows. Very risky, but often where reflex bounces can start.

🔗 Chart
https://www.tradingview.com/chart/?symbol=AAPL
```

### Momentum Reversal (Bullish example)
```
🔄 MOMENTUM REVERSAL — TSLA
🕒 01-01-2026 · 02:10 PM EST

💰 Price Path
• Last: $226.40 (-1.2% today, from -5.0% low)
• O $234.00 · H $236.20 · L $224.10 · C $226.40
• RVOL: 1.9×

📈 Reversal Context
• Earlier: BELOW VWAP → Now ABOVE VWAP (reclaimed ~45% of range)
• RSI(14, 5m): 28.4 → 42.1 (recovering)

🧠 Read
Intraday reversal after a hard selloff — buyers reclaimed VWAP and are pushing off the lows.

🔗 Chart
https://www.tradingview.com/chart/?symbol=TSLA
```
(Bearish variant swaps to VWAP loss, lower highs, RSI rollover.)

### RSI Oversold
```
🧠 RSI OVERSOLD — META
🕒 01:35 PM EST · 01-01-2026

💰 Price Snapshot
• Last: $310.22 (-3.1% today)
• O $320.10 · H $322.80 · L $308.60 · C $310.22

📉 Momentum
• RSI(14, 5m): 23.4 (≤ 30 OVERSOLD)
• RVOL: 1.6×
• Distance from Low: 0.5%

🧠 Read
Short-term momentum washed out — potential bounce/mean reversion area.

🔗 Chart
https://www.tradingview.com/chart/?symbol=META
```

### RSI Overbought
```
🔥 RSI OVERBOUGHT — LLY
🕒 10:50 AM EST · 01-01-2026

💰 Price Snapshot
• Last: $720.10 (+3.9% today)
• O $695.80 · H $722.40 · L $694.50 · C $720.10

📈 Momentum
• RSI(14, 5m): 81.2 (≥ 70 OVERBOUGHT)
• RVOL: 1.9×
• Distance from High: 0.4%

🧠 Read
Momentum is very stretched — potential fade or consolidation zone.

🔗 Chart
https://www.tradingview.com/chart/?symbol=LLY
```

### Opening Range Breakout (Long)
```
⚡️ OPENING RANGE BREAKOUT — NVDA
🕒 01-01-2026 · 09:47 AM EST
────────────
🚀 LONG Breakout Above Opening Range High
💰 Last: $522.30 (+3.4% vs prior close)

📊 Opening Range (first 15m)
• High: $510.00
• Low: $500.20

🔥 Break Distance: +2.4% above OR high

📈 Volume & Strength
• Volume: 12,500,000 (2.1× avg)
• Dollar Vol ≈ $6,520,000,000
• RVOL: 2.1×
• VWAP: $518.10 (trading ABOVE VWAP)

🔎 Context
Strong OR breakout with confirmed volume & trend strength

🔗 Chart
https://www.tradingview.com/chart/?symbol=NVDA
```
(Breakdown swaps 🩸 SHORT, below OR low, VWAP BELOW, negative break distance.)

### Squeeze Bot
```
🧨 SQUEEZE BREAKOUT — SHOP
🕒 01-01-2026 · 01:20 PM EST

💰 Price Snapshot
• Last: $82.40 (+2.9% today)
• O $79.10 · H $83.00 · L $78.60 · C $82.40
• RVOL: 1.5×

📉 Compression Phase
• Bollinger Band Width: 1.8% of price (near recent lows)
• Daily range compression flagged over 5 sessions

📈 Breakout Context
• Break direction: UPSIDE (closing above upper band)
• Above VWAP: YES
• Recent swing high: $81.90 (now cleared)

🧠 Read
Volatility squeeze resolving to the upside with volume starting to expand — classic pre-breakout to breakout transition.

🔗 Chart
https://www.tradingview.com/chart/?symbol=SHOP
```

### Dark Pool Radar
```
🕳️ DARK POOL RADAR — AAPL
🕒 01-01-2026 · 02:15 PM EST
💰 Underlying: $182.40 · Day Move: -1.4% · RVOL: 1.3×
────────────
🧊 Window: last 30 min (today only)
📦 Prints: 42
💵 Dark Pool Notional (window): ≈ $310,000,000
🐋 Largest Print: ≈ $45,000,000 @ $182.10
📊 Dark Pool vs Full-Day Volume: 8.5% of today’s $ volume
🔍 Context: Cluster of mid-day blocks accumulating just below VWAP.
🔗 Chart: https://www.tradingview.com/chart/?symbol=AAPL
```

### Earnings Scanner
```
📅 EARNINGS RADAR — NFLX
🕒 01-01-2026 · 03:00 PM EST
────────────
• Earnings Date: 01-05-2026 (after close)
• Price: $502.10 (+1.2%)
• IV Snapshot: elevated vs baseline
🧠 Read: Upcoming event within 4 days; watch for IV crush setups
🔗 Chart: https://www.tradingview.com/chart/?symbol=NFLX
```

### Daily Ideas (Longs / Shorts)
```
💡 DAILY IDEAS — LONGS
🕒 01-01-2026 · 10:52 AM EST
────────────
Top LONG ideas (ranked by confluence score):

NVDA — Score: 9.1
   Trend: Uptrend (price > MA20 > MA50)
   💵 Price: $522.30 (O: $510.00, H: $525.40, L: $508.20)
   📊 Intraday: +2.4% vs prior close, above VWAP | RVOL 2.1×
   🔍 RSI (5m): 54.2
   🧩 Options flow bias: +0.72
   🧠 Read: High confluence across trend, volume, RSI, flow
   📈 Chart: https://www.tradingview.com/chart/?symbol=NVDA
```
(Shorts version swaps direction/bias; “No ideas” variants state none found.)

All options alerts were fully redesigned for readability: parsed contracts, EST timestamps, human-readable premiums/notional, and clear context/bias lines.

### Options Cheap Flow (💰)
```
💰 CHEAP FLOW — QID
🕒 01-01-2026 · 02:25 PM EST
💵 Underlying: $18.42 (+2.1% today)
────────────
🎯 Order: 250x 01-16-2026 19C (Strike $19.00)
⏳ Tenor: 15 DTE
💸 Premium per contract: $0.18 (below CHEAP_MAX_PREMIUM=$0.80)
💰 Total Notional: $4,500 (meets CHEAP_MIN_NOTIONAL; size meets CHEAP_MIN_SIZE)
📊 Structure: near-dated · OTM call · sized at 250 contracts
⚖️ Context: Option volume 3,200 vs OI 1,000 (3.2× OI)
🧠 Read: Speculative bullish “lottery” flow priced cheaply.
🔗 Chart: https://www.tradingview.com/chart/?symbol=QID
```

### Options Unusual Flow (⚠️)
```
⚠️ UNUSUAL FLOW — TSLA
🕒 01-01-2026 · 02:20 PM EST
💰 Underlying: $252.40 (+3.1% today) · RVOL 4.5×
────────────
🎯 Order: 75x 01-16-2026 260C (Strike $260.00)
💸 Premium per contract: $4.80 · Total Notional: $153,600
📊 Unusual vs normal:
• Option volume today: 2,300 (avg 120)
• This trade: 75 contracts (3.3% of today’s option volume)
• Volume vs OI: 2,300 vs 400 (5.8× OI)
🧠 Flow tags: SWEEP · AT_ASK · SAME_DAY_CLUSTER
📌 Narrative: Short-dated upside call flow well above normal activity.
🔗 Chart: https://www.tradingview.com/chart/?symbol=TSLA
```

### Options Whale Flow (🐳)
```
🐳 WHALE FLOW — BDX
🕒 01-01-2026 · 02:21 PM EST
💰 Underlying: $245.32 (+1.8% today) · RVOL 2.4×
────────────
📦 Order: 100x 01-16-2026 130C (Strike $130.00) (⏳ 15 DTE)
💵 Premium per contract: $6.52 · Total Notional: $652,000
📊 Flow tags: WHALE_SIZE · SHORT_DTE
⚖️ Context: Option volume 1,200 vs OI 3,400 (0.4× OI)
🧠 Read: Aggressive bullish whale flow.
🔗 Chart: https://www.tradingview.com/chart/?symbol=BDX
```

### Options IV Crush (🔥)
```
🔥 IV CRUSH — AMD
🕒 01-02-2026 · 09:45 AM EST
💰 Underlying: $112.10 (-6.2% today)
────────────
🎯 Contract: 150x 01-17-2026 115C (Strike $115.00)
💸 Premium per contract: $1.20 · Total Notional: $18,000
📉 IV Crush Details:
• IV before: 142% → IV now: 82%
• IV drop: -60.0% (meets IVCRUSH_MIN_IV_DROP_PCT=20%)
• Option volume: 2,100 (meets IVCRUSH_MIN_VOL)
🧠 Context: Post-event IV collapse with price stabilizing
⚖️ Risk View: Elevated realized move already happened; options now pricing less future volatility.
🔗 Chart: https://www.tradingview.com/chart/?symbol=AMD
```

### Options Indicator (Analytics)
```
🧠 OPTIONS_INDICATOR — SPY
🕒 01-01-2026 · 02:52 PM EST
💰 Underlying: $475.10 · RVOL 1.3×
────────────
🎯 Regime: HIGH-IV MOMENTUM
📊 IV Rank (intra-chain): 78
📉 RSI(14): 64.2
📈 MACD: 0.123 vs Signal 0.087
📎 Bollinger 20/2: Lower 460.00 · Mid 470.00 · Upper 480.00
💵 Dollar Volume (today): ≈ $8,200,000,000
📦 Options OI: total 2,500,000 · max strike 180,000
📊 Day Move: 1.8%

🧠 Bias: Bullish momentum — continuing strength vs vol regime
🔗 Chart: https://www.tradingview.com/chart/?symbol=SPY
```
## 4️⃣ Status Report & Heartbeat
- **Source**: `bots/status_report.py` reads `STATUS_STATS_PATH`, filters to today’s trading day (EST), and builds the MoneySignalAI Heartbeat.
- **What it shows**:
  - **Bots**: last run time or “No run today”.
  - **Totals**: sum of today’s scanned/matches/alerts across all bots.
  - **Per Bot**: today’s scanned | matches | alerts.
  - **Diagnostics**: high-scan/zero-alert, ran-today-zero-scans, not-run-today.
  - **Runtime**: median + last runtime (n runs today).
- **Use it to**:
  - Verify bots are running on schedule.
  - Spot over-filtering (high scan, zero alerts).
  - Catch wiring issues (zero scans) or disabled bots.

---

## 5️⃣ Installation & Setup

### Prerequisites
- Python 3.10+
- Polygon/Massive-compatible API key (`POLYGON_KEY`)
- Telegram tokens: `TELEGRAM_TOKEN_ALERTS`, `TELEGRAM_TOKEN_STATUS`, `TELEGRAM_CHAT_ALL`

### Clone & Install
```bash
git clone https://github.com/moneysignalai/money-signal-ai-polygon.git
cd money-signal-ai-polygon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration (.env)
Set env vars (non-exhaustive):
- **Universe**: `TICKER_UNIVERSE`, `DYNAMIC_MAX_TICKERS` (cap ~1,500), `DYNAMIC_VOLUME_COVERAGE`, `FALLBACK_TICKER_UNIVERSE`
- **Global floors**: `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`
- **Premarket**: `MIN_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, `MIN_PREMARKET_PRICE`
- **ORB**: `ORB_RANGE_MINUTES`, `ORB_MIN_DOLLAR_VOL`, `ORB_MIN_RVOL`, `ORB_START_MINUTE`, `ORB_END_MINUTE`
- **RSI**: `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `RSI_MIN_PRICE`, `RSI_MIN_DOLLAR_VOL`, `RSI_MAX_UNIVERSE`
- **Panic Flush / Momentum Reversal**: `PANIC_FLUSH_*`, `MOMO_REV_*`
- **Trend/Swing**: `TREND_RIDER_*`, `SWING_*`
- **Squeeze**: `SQUEEZE_*`
- **Dark Pool**: `DARK_POOL_MIN_NOTIONAL`, `DARK_POOL_MIN_LARGEST_PRINT`, `DARK_POOL_LOOKBACK_MINUTES`
- **Options**: `OPTIONS_FLOW_MAX_UNIVERSE`, `OPTIONS_MIN_UNDERLYING_PRICE`, `CHEAP_*`, `UNUSUAL_*`, `WHALES_*`, `IVCRUSH_*`
- **Operational**: `BOT_TIMEOUT_SECONDS`, `SCAN_INTERVAL_SECONDS`, `STATUS_HEARTBEAT_INTERVAL_MIN`, `STATUS_STATS_PATH`, `OPTIONS_IV_CACHE_PATH`, `DEBUG_FLOW_REASONS`, `DISABLED_BOTS`, `TEST_MODE_BOTS`

### Run Locally
```bash
python main.py
# or
uvicorn main:app --reload
```
Bots start scanning per schedule; heartbeat posts every `STATUS_HEARTBEAT_INTERVAL_MIN` minutes.

### Deploy (Render / Docker)
- Configure env vars in Render dashboard.
- Deploy as a web/background worker using this repo; container builds from `requirements.txt`.
- GitHub-connected deploys auto-restart with new commits.

---

## 6️⃣ Performance Philosophy
- **Reliability at scale**: dynamic universes capped at ~1,500, per-bot timeouts, error isolation per symbol.
- **Efficiency**: shared caches/helpers, day-scoped data, debounced alerts.
- **Data accuracy**: today-only flows/prints for intraday/option/dark-pool strategies; EST-aware trading-day logic.
- **Observability**: heartbeat diagnostics expose over-filtering (high scan, zero alerts) and zero-scan runs.

---

## 7️⃣ Roadmap / Future Enhancements
- ML-driven probability scoring and quality tiers
- Backtesting and performance analytics per bot
- Web dashboard for alert review and tuning
- Multi-account routing and broker integration
- Expanded analytics (sector/relative-strength overlays, pair trades)

---

MoneySignalAI delivers production-quality, emoji-rich alerts and transparent telemetry so traders, investors, and engineers can trust the signals and scale their workflows.
