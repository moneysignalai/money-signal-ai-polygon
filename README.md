# MoneySignalAI — Polygon Data Engine

MoneySignalAI is an institutional-grade, Python-native equities and options alerting platform. It watches a dynamic top-volume universe (up to ~1,500 tickers with `TICKER_UNIVERSE` fallback), applies strategy-specific filters, and streams emoji-rich, trader-ready alerts to Telegram. The FastAPI scheduler runs multiple bots in parallel, enforces time windows, and emits a heartbeat with per-bot health, scan counts, and runtimes.

- **AI-powered, modular bot engine** across equity momentum, intraday flows, gaps, squeezes, dark pool, earnings, options flow, and analytics.
- **Real-time Polygon/Massive data** with EST-aware trading-day logic.
- **Env-driven tuning** for every threshold (RVOL, dollar volume, IV crush %, DTE, gap %, RSI bands, etc.).
- **Production telemetry** via `status_report.py` (today-only stats, diagnostics, runtimes) and Telegram delivery.
- **Scales to 1,500+ tickers** with dynamic top-volume universes and safe fallbacks.

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
  - OCC parsing, contract display, IV/notional/DTE helpers, and shared `format_option_alert` used by all option flow bots.
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
- **Volume Monster** – Intraday “monster bar” spikes with strong price moves. Env: `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `VOLUME_MONSTER_RVOL`, `VOLUME_MONSTER_MIN_MOVE_PCT`, global floors. Runs RTH.
- **Gap Flow** – Gap up/down vs prior close with RVOL/liquidity filters. Env: `GAP_FLOW_MAX_UNIVERSE` + global gap/volume floors. Runs RTH.
- **Trend Rider** – Breakouts in strong uptrends (stacked MAs, new highs). Env: `TREND_RIDER_MIN_DOLLAR_VOL`, `TREND_RIDER_MIN_RVOL`, `TREND_RIDER_TREND_DAYS`, `TREND_RIDER_MIN_BREAKOUT_PCT`, global floors. Runs RTH.
- **Swing Pullback** – Dip-buys inside uptrends near moving averages. Env: `SWING_*` thresholds (pullback %, trend days, RVOL, dollar vol), global floors. Runs RTH.
- **Panic Flush** – Capitulation-style down days near lows with heavy RVOL. Env: `PANIC_FLUSH_MIN_DROP`, `PANIC_FLUSH_MIN_RVOL`, `PANIC_FLUSH_MAX_FROM_LOW_PCT`, global floors. Runs RTH.
- **Momentum Reversal** – Large intraday moves that start reversing (mean reversion). Env: `MOMO_REV_MIN_RECLAIM_PCT`, `MOMO_REV_MIN_RVOL`, `MOMO_REV_MIN_MOVE_PCT`, `MOMO_REV_MAX_FROM_EXTREME_PCT`, global floors. Runs RTH.
- **RSI Signals** – Overbought/oversold signals on intraday RSI with liquidity filters. Env: `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `RSI_MIN_PRICE`, `RSI_MIN_DOLLAR_VOL`, `RSI_MAX_UNIVERSE`, global floors. Runs RTH.
- **Opening Range Breakout (ORB)** – Breaks above/below opening range with retest + FVG context. Env: `ORB_RANGE_MINUTES`, `ORB_MIN_DOLLAR_VOL`, `ORB_MIN_RVOL`, `ORB_START_MINUTE`, `ORB_END_MINUTE`, global floors. Runs RTH opening window.
- **Squeeze Bot** – Price/volume acceleration resembling short-squeeze behavior (no short-interest feed). Env: `SQUEEZE_*` thresholds, global floors. Runs RTH.
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
Real template examples mirroring current code output. Timestamps are EST with `format_est_timestamp` or `now_est` formatting per bot.

### Premarket Scanner
```
🧠 PREMARKET — MDB
💰 $382.40 · 📊 RVOL 1.8x
────────────
📈 Prev Close: $365.10 → Premarket Last: $382.40
📊 Premarket Range: $378.00 – $386.20
📦 Premarket Vol: 1,120,000 (≈ $428,000,000)
💰 Day Vol (partial): 850,000 (≈ $325,000,000)
📊 RVOL (partial): 1.8x
🎯 Grade: A-
🧠 Bias: Watch for RTH follow-through
🔗 Chart: https://www.tradingview.com/chart/?symbol=MDB
```

### Volume Monster
```
🚨 VOLUME MONSTER — AXSM (12-30-2025 · 02:21 PM EST)
────────────
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410 (6.3x avg)
• 💰 Dollar Vol: $558,770,642
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

### Gap Flow (Gap Up)
```
🚀 GAP FLOW — AXSM (12-30-2025 · 09:45 AM EST)
────────────
• Direction: Gap Up (🔼 +6.5% vs prior close)
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410
• 💰 Dollar Vol: $484,885,891
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```
(Gap Down swaps 🔻/negative gap.)

### Trend Rider
```
🚀 TREND RIDER — NVDA
🕒 01-01-2026 · 02:15 PM EST

💰 Price + Volume
• Last: $522.88 (+4.2% UP)
• RVOL: 2.1×
• Dollar Vol: $8,200,000,000

📈 Trend Structure
• Breakout vs 20-day high: $510.20
• 50 SMA: above 50SMA
• 200 SMA: above 200SMA
• Today’s range: O $500.10 · H $525.40 · L $497.50 · C $522.88

🧠 Read
Strong trend, stacked MAs, fresh breakout.

🔗 Chart
https://www.tradingview.com/chart/?symbol=NVDA
```

### Swing Pullback
```
🧠 SWING_PULLBACK — AAPL
💰 Last: $182.50
📊 RVOL: 1.4x
────────────
SWING PULLBACK — AAPL
• Price: $182.50 (pullback 4.2% from swing high)
• MAs: MA20 178.40 | MA50 170.25
• Volume: 1.4× avg — Dollar Vol $850,000,000
• Link: https://www.tradingview.com/chart/?symbol=AAPL
```

### Panic Flush
```
⚠️ PANIC FLUSH — AAPL
🕒 01-01-2026 · 01:45 PM EST

💰 Price + Volume
• Last: $182.10 (-4.8% DOWN)
• From Open: -6.2% DOWN
• RVOL: 3.4×
• Volume: 78,200,000
• Dollar Vol: $14,200,000,000

📉 Intraday Damage
• O $194.00 · H $195.10 · L $180.55 · C $182.10
• Closing Near Lows? Yes
• Multi-day context: pressing into recent lows near $180.55

📈 VWAP & Structure
• VWAP: $188.20 (trading well below VWAP)
• Day structure: heavy intraday selloff, near session lows with capitulation-style volume

🔎 Reference levels
• Support: today’s low $180.55 and prior day low $184.20
• Resistance: VWAP $188.20, bounce high $186.90

🧠 Read
Violent sell pressure with elevated liquidity. Possible capitulation / flush zone for contrarian setups.

🔗 Chart
https://www.tradingview.com/chart/?symbol=AAPL
```

### Momentum Reversal (Bullish example)
```
🧠 MOMENTUM_REVERSAL BULLISH — TSLA
💰 Last: $242.10
📊 RVOL: 2.0x
────────────
• Last: $242.10
• Initial move: -7.4% from open
• Reclaim: 45.0% of initial move
• Volume: 18,500,000 (2.0× avg) — Dollar Vol: $4,477,000,000
• Context: Strong intraday reversal (bullish).
• Chart: https://www.tradingview.com/chart/?symbol=TSLA
```
(Bearish variant changes header suffix to `BEARISH` and context.)

### RSI Oversold
```
🧠 RSI OVERSOLD — TSLA
🕒 01-01-2026 · 02:20 PM EST

💰 Price Snapshot
• Last: $226.40 (-3.4% DOWN)
• RVOL: 1.9×
• Dollar Vol: $6,400,000,000

📉 Momentum Setup
• RSI(14, 5m): 24.3 (≤ 30 OVERSOLD)
• Today’s range: O $234.00 · H $236.20 · L $224.10 · C $226.40
• Distance from Low: 1.0%

🧠 Read
Short-term momentum washed out. Potential bounce / mean-reversion zone.

🔗 Chart
https://www.tradingview.com/chart/?symbol=TSLA
```

### RSI Overbought
```
🔥 RSI OVERBOUGHT — META
🕒 01-01-2026 · 02:20 PM EST

💰 Price Snapshot
• Last: $410.22 (+3.8% UP)
• RVOL: 2.0×
• Dollar Vol: $3,900,000,000

📈 Momentum Setup
• RSI(14, 5m): 79.2 (≥ 70 OVERBOUGHT)
• Today’s range: O $395.10 · H $411.80 · L $392.20 · C $410.22
• Distance from High: 0.4%

🧠 Read
Short-term move looks stretched. Possible fade / digestion zone.

🔗 Chart
https://www.tradingview.com/chart/?symbol=META
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

• Reference levels:
  - Support zone (near-term): $505.00
  - Resistance zone: $525.40

🔗 Chart:
https://www.tradingview.com/chart/?symbol=NVDA
```
(Breakdown swaps 🩸 SHORT, below OR low, VWAP BELOW, etc.)

### Squeeze Bot
```
🧠 SQUEEZE — GME
💰 Last: $38.40
📊 RVOL: 3.2x
────────────
SQUEEZE RADAR — GME
• Last: $38.40 (+12.5% vs close, +9.2% from open)
• Volume: 12,300,000 (3.2× avg) — Dollar Vol: $471,000,000
• Near HOD: 1.2% off high
• Context: Strong up move with heavy volume; potential squeeze continuation.
• Chart: https://www.tradingview.com/chart/?symbol=GME
```

### Dark Pool Radar
```
🕳️ DARK POOL RADAR — AAPL
01-01-2026 · 02:15 PM EST
💰 Underlying: $182.40 · Day Move: -1.4% · RVOL: 1.3x
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
🧠 EARNINGS — NFLX
💰 Last: $502.10
📊 RVOL: 1.4x
────────────
💎 EARNINGS MOVE — NFLX
🕒 10:30 AM EST · Nov 20
💰 Price: $502.10
📊 Move: 6.4% · Gap: 5.1% · Intraday: 1.2%
📦 Vol: 5,200,000 (≈ $2,615,000,000) · RVOL: 1.4x
🎯 Grade: A-
📰 Earnings: 11-20-2025 AMC
🔗 Chart: https://www.tradingview.com/chart/?symbol=NFLX
```

### Daily Ideas (Longs / Shorts)
```
💡 DAILY IDEAS — LONGS (01-01-2026 · 10:52 AM EST)
────────────
Top 3 LONG ideas (ranked by confluence score):

NVDA — Score: 9.1
   Trend: Uptrend (price > MA20 > MA50)
   💵 Price: $522.30 (O: $510.00, H: $525.40, L: $508.20)
   📊 Intraday: +2.4% vs prior close, above VWAP | RVOL 2.1x
   🔍 RSI (5m): 54.2
   🧩 Options flow bias: +0.72
   📈 Chart: https://www.tradingview.com/chart/?symbol=NVDA
```
(Shorts version swaps direction and bias; “No ideas” variants state no high-confluence ideas.)

### Options Cheap Flow (💰)
```
💰 CHEAP FLOW — QID (12-30-2025 · 02:21 PM EST)
────────────
• Contract: QID 9C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $16.20
• 💰 Premium: $0.35 | Size: 100 | Notional: $3,500
• 📊 IV: 41.7% | Volume: 800 | OI: 1,900
• 📈 Chart: https://www.tradingview.com/chart/?symbol=QID
```

### Options Unusual Flow (⚠️)
```
⚠️ UNUSUAL FLOW — TSLA (12-30-2025 · 02:21 PM EST)
────────────
• Contract: TSLA 260C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $252.40
• 💰 Premium: $4.80 | Size: 320 | Notional: $153,600
• 📊 IV: 54.2% | Volume: 4,200 | OI: 7,900
• 📈 Chart: https://www.tradingview.com/chart/?symbol=TSLA
```

### Options Whale Flow (🐳)
```
🐳 WHALE FLOW — BDX (12-30-2025 · 02:21 PM EST)
────────────
• Contract: BDX 130C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $129.82
• 💰 Premium: $6.52 | Size: 100 | Notional: $652,000
• 📊 IV: 34.2% | Volume: 1,200 | OI: 3,400
• 📈 Chart: https://www.tradingview.com/chart/?symbol=BDX
```

### Options IV Crush (🔥)
```
🔥 IV CRUSH — AMD (12-30-2025 · 02:21 PM EST)
────────────
• Contract: AMD 110C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $112.10
• 💰 Premium: $2.45 | Size: 540 | Notional: $132,300
• 📊 IV: 64.0% | Volume: 3,100 | OI: 5,800
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AMD
```

### Options Indicator (Analytics)
```
🧠 OPTIONS_INDICATOR — SPY
💰 Last: $475.10
📊 RVOL: 1.3x
────────────
📈 OPTIONS INDICATOR — SPY
🕒 02:52 PM EST · 01-01-2026
💰 Underlying: $475.10 · RVOL 1.3x
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

### Earnings (already above) and other bots use similar formats; see messages above for concrete structures.

---

## 4️⃣ Status Report & Heartbeat
- **Source**: `bots/status_report.py` reads `STATUS_STATS_PATH`, filters to today’s trading day (EST), and builds the MoneySignalAI Heartbeat.
- **What it shows**:
  - **Bots**: last run time or “No run today”.
  - **Totals**: sum of today’s scanned/matches/alerts across all bots.
  - **Per Bot**: today’s scanned | matches | alerts.
  - **Diagnostics**: high-scan/zero-alert; ran-today-zero-scans; not-run-today.
  - **Runtime**: median + last runtime (today) with sample size.
- **Interpretation**:
  - **Scanned** – symbols/contracts evaluated today.
  - **Matches** – candidates that passed filters today.
  - **Alerts** – Telegram messages sent today.
- **Example heartbeat (conceptual)**:
```
📡 MoneySignalAI Heartbeat · 3:00 PM EST · Jan 01
✅ ALL SYSTEMS GOOD

🤖 Bots
• Volume Monster …… 🟢 01-01-2026 · 02:53 PM EST
• Gap Flow …………… 🟢 01-01-2026 · 02:54 PM EST
• Trend Rider …… 🟢 01-01-2026 · 02:57 PM EST
• RSI Signals …… 🟢 01-01-2026 · 02:58 PM EST
• Options Cheap Flow …… 🟢 01-01-2026 · 02:45 PM EST
... (others) ...

📊 Totals
• Scanned: 11,811 • Matches: 20 • Alerts: 20

📈 Per Bot (scanned | matches | alerts)
• Volume Monster …… 1,425 | 6 | 6
• Gap Flow …………… 1,425 | 9 | 9
• Trend Rider …… 1,425 | 0 | 0
• RSI Signals …… 1,425 | 0 | 0
• Options Cheap Flow …… 500 | 2 | 2
... (others) ...

🛠 Diagnostics
• High scan, zero alerts: Dark Pool, RSI Signals, Squeeze, Swing Pullback, Trend Rider
• Ran today, zero scans (check universes/filters): (if any)
• Not run today: (if any)

⏱ Runtime (today)
• Volume Monster …… median 45.3s (last 45.3s, n=3)
• Gap Flow …………… median 45.1s (last 45.1s, n=3)
... (others) ...
```

---

## 5️⃣ Installation & Setup

### Prerequisites
- Python 3.10+ (per `requirements.txt`).
- Polygon/Massive API key via `POLYGON_KEY`.
- Telegram tokens: `TELEGRAM_TOKEN_ALERTS`, `TELEGRAM_TOKEN_STATUS` (optional), `TELEGRAM_CHAT_ALL`.

### Clone & Install
```bash
git clone https://github.com/moneysignalai/money-signal-ai-polygon.git
cd money-signal-ai-polygon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configure (.env)
Key groups (see code for defaults):
- **Universe & pacing**: `TICKER_UNIVERSE`, `FALLBACK_TICKER_UNIVERSE`, `DYNAMIC_MAX_TICKERS` (cap 1500), `DYNAMIC_VOLUME_COVERAGE`, `DYNAMIC_MAX_LOOKBACK_DAYS`, `SCAN_INTERVAL_SECONDS`, `BOT_TIMEOUT_SECONDS`.
- **Global floors**: `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`.
- **Session gates**: `DISABLED_BOTS`, `TEST_MODE_BOTS`, `DEBUG_FLOW_REASONS`, `STATUS_HEARTBEAT_INTERVAL_MIN`, `STATUS_STATS_PATH`.
- **Premarket**: `MIN_PREMARKET_*`, `PREMARKET_TICKER_UNIVERSE`, `PREMARKET_ALLOW_OUTSIDE_WINDOW`.
- **Equity bots**: `VOLUME_MONSTER_*`, `GAP_FLOW_MAX_UNIVERSE`, `TREND_RIDER_*`, `SWING_*`, `PANIC_FLUSH_*`, `MOMO_REV_*`, `RSI_*`, `SQUEEZE_*`, `ORB_*`.
- **Options**: `OPTIONS_FLOW_MAX_UNIVERSE`, `OPTIONS_FLOW_TICKER_UNIVERSE`, `OPTIONS_MIN_UNDERLYING_PRICE`, `CHEAP_*`, `UNUSUAL_*`, `WHALES_*`, `IVCRUSH_*`, `OPTIONS_IV_CACHE_PATH`.
- **Earnings/Daily Ideas**: `EARNINGS_MAX_FORWARD_DAYS`, `DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW`.

### Run Locally
- Scheduler/API: `python main.py` (or `uvicorn main:app --reload` if exposing FastAPI endpoints).
- Heartbeat posts every `STATUS_HEARTBEAT_INTERVAL_MIN` minutes to `TELEGRAM_CHAT_ALL`.

### Deploy (Render/Docker)
- Use `render.yaml` as a reference; set env vars in Render dashboard.
- Dockerize by wrapping `python main.py` or `uvicorn main:app`; supply the same env map.

---

## 6️⃣ Performance Philosophy
- **Reliability at scale**: Dynamic top-volume universes, per-bot timeouts, per-symbol try/except, day-scoped data (no stale prints/flows).
- **Efficiency**: Shared caches/helpers, debounced alerts, minimal HTTP calls per run.
- **Data accuracy**: EST-aware trading-day filters; today-only flow/dark-pool scans; env-tuned floors to avoid noise.
- **Observability**: Heartbeat diagnostics surface over-filtering, zero-scan runs, and runtime regressions.

---

## 7️⃣ Roadmap / Future Enhancements
- ML-driven probability scoring and quality tiers
- Backtesting/performance analytics per bot
- Web dashboard for alert review/tuning
- Multi-account routing and broker/API execution hooks
- Sector/relative-strength overlays and pair-trade scaffolding

---

MoneySignalAI delivers production-quality, emoji-rich alerts and transparent telemetry so traders, investors, and engineers can trust the signals and scale their workflows.
