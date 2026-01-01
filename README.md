# MoneySignalAI

MoneySignalAI is a production-grade, multi-bot equities and options scanner that streams structured, human-readable trade ideas to Telegram. A FastAPI scheduler coordinates independently tuned strategies that watch up to ~1,500 tickers (dynamic top-volume universe with static `TICKER_UNIVERSE` fallback), enforce per-bot time windows, and emit a heartbeat with per-bot health, scan counts, and runtimes.

## Key Features
- **Modular bot catalog** spanning equity trend/momentum, intraday flow, gaps, squeezes, dark pool activity, earnings, daily ideas, and dedicated options flow families (cheap, unusual, whales, IV crush).
- **Dynamic universes** – shared resolver pulls the top-by-volume universe (capped at 1,500) with fallbacks to `TICKER_UNIVERSE` and per-bot overrides.
- **Env-driven tuning** – every threshold (RVOL, dollar volume, IV drops, DTE, gap %, RSI bands, etc.) is controlled via the `.env` used in Render.
- **Production telemetry** – a status/heartbeat bot aggregates today-only stats (scanned, matches, alerts, runtimes) and surfaces diagnostics like “high scan, zero alerts”.
- **Human-readable alerts** – consistent emojis, MM-DD-YYYY · HH:MM AM/PM EST timestamps, TradingView chart links, and readable option contracts.
- **Deploy anywhere** – run locally (`python main.py` / `uvicorn main:app`), package into Docker, or deploy to Render with the same env map.

---
## Architecture & Project Layout
- **Scheduler / FastAPI (`main.py`)** – registry of bots (name, module, async entrypoint, interval). Applies `DISABLED_BOTS`, `TEST_MODE_BOTS`, and RTH/premarket windows before launching bots with timeouts.
- **Shared utilities (`bots/shared.py`)** – EST time helpers, trading-day checks, dynamic universe resolution (top-volume + fallbacks), RVOL/volume helpers, Telegram senders (`send_alert`, `send_alert_text`), TradingView `chart_link`, stats helpers (`record_bot_stats`, `record_error`), and formatting helpers (EST timestamps, option parsing).
- **Options utilities (`bots/options_common.py`)** – option contract parsing/formatting, OCC decoding, DTE/IV/notional helpers, and the shared `format_option_alert` used by all option flow bots.
- **Bots (`bots/*.py`)** – each strategy exposes an async `run_*` entrypoint and uses shared helpers for universes, RTH windows, alerts, and stats.
- **Status / Heartbeat (`bots/status_report.py`)** – reads today’s stats, classifies bots (ran, zero scans, not run), computes today-only totals/runtimes, and sends the “MoneySignalAI Heartbeat” to Telegram.
- **Scripts (`scripts/smoke_test.py`)** – lightweight import/run smoke checks.

Directory outline:
```
main.py                     # FastAPI app + scheduler
bots/                       # Strategy modules and helpers
  shared.py                 # Time, universe, Telegram, stats helpers
  options_common.py         # Shared option parsing/formatting
  volume_monster.py
  gap_flow.py
  swing_pullback.py
  trend_rider.py
  panic_flush.py
  momentum_reversal.py
  rsi_signals.py
  openingrangebreakout.py
  squeeze.py
  dark_pool_radar.py
  earnings.py
  daily_ideas.py
  premarket.py
  options_cheap_flow.py
  options_unusual_flow.py
  options_whales.py
  options_iv_crush.py
  options_indicator.py
  ...
bots/status_report.py       # Heartbeat + stats aggregation
render.yaml                 # Render deployment config
requirements.txt            # Python dependencies
scripts/smoke_test.py       # Simple bot import/run smoke test
```

---
## Getting Started

### Prerequisites
- Python 3.10+ (align with `requirements.txt`).
- Massive/Polygon-compatible API key via `POLYGON_KEY`.
- Telegram bot tokens:
  - `TELEGRAM_TOKEN_ALERTS` (alerts channel)
  - `TELEGRAM_TOKEN_STATUS` (heartbeat; falls back to alerts if unset)
  - `TELEGRAM_CHAT_ALL` (chat ID for alerts/heartbeat)

### Clone & Install
```bash
git clone <repo-url>
cd money-signal-ai-polygon
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration (.env)
Core envs (see code for defaults):
- **Universe & pacing**: `TICKER_UNIVERSE`, `FALLBACK_TICKER_UNIVERSE`, `DYNAMIC_MAX_TICKERS` (capped at 1500), `DYNAMIC_VOLUME_COVERAGE`, `DYNAMIC_MAX_LOOKBACK_DAYS`, `SCAN_INTERVAL_SECONDS`, `BOT_TIMEOUT_SECONDS`.
- **Global floors**: `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`.
- **Session gates**: `DISABLED_BOTS`, `TEST_MODE_BOTS`, `DEBUG_FLOW_REASONS`, `DEBUG_PING_ENABLED`, `DEBUG_STATUS_PING_ENABLED`, `STATUS_HEARTBEAT_INTERVAL_MIN`, `STATUS_STATS_PATH`.
- **Premarket**: `MIN_PREMARKET_PRICE`, `MIN_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, `PREMARKET_TICKER_UNIVERSE`, `PREMARKET_ALLOW_OUTSIDE_WINDOW`.
- **Equity bots**: `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `VOLUME_MONSTER_RVOL`, `PANIC_FLUSH_MIN_DROP`, `PANIC_FLUSH_MIN_RVOL`, `MOMO_REV_MIN_MOVE_PCT`, `MOMO_REV_MIN_RECLAIM_PCT`, `TREND_RIDER_MIN_DOLLAR_VOL`, `TREND_RIDER_MIN_RVOL`, `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `SQUEEZE_*`, `ORB_*`, `GAP_FLOW_MAX_UNIVERSE`, etc.
- **Options**: `OPTIONS_FLOW_MAX_UNIVERSE`, `OPTIONS_FLOW_TICKER_UNIVERSE`, `OPTIONS_MIN_UNDERLYING_PRICE`, `CHEAP_*`, `UNUSUAL_*`, `WHALES_*`, `IVCRUSH_*`, `OPTIONS_IV_CACHE_PATH`.
- **Earnings/Daily**: `EARNINGS_MAX_FORWARD_DAYS`, `DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW`.

Tuning guidance: raise thresholds to reduce alert volume; lower thresholds to increase idea flow. Adjust per-bot `*_ALLOW_OUTSIDE_RTH` flags for debugging outside normal hours.

### Running Locally
- Scheduler + API: `python main.py` (or `uvicorn main:app --reload`).
- Bots start on their configured intervals; heartbeat messages appear every `STATUS_HEARTBEAT_INTERVAL_MIN` minutes in the status Telegram chat.

### Deploying (Render / Docker)
- Use `render.yaml` as a template; set env vars in the Render dashboard.
- Docker: create a simple Dockerfile wrapping `python main.py` or `uvicorn main:app` and supply the same env map.

---
## Bots & Alert Logic
Each bot uses the shared universe resolver (dynamic top-volume up to 1,500 with `TICKER_UNIVERSE` fallback), session gating (RTH/premarket), shared Telegram helpers, and `record_bot_stats` for heartbeat telemetry.

### Equity / Stock Bots
#### Volume Monster
- **Purpose**: Flags outsized intraday volume spikes with strong price action.
- **Env**: `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `VOLUME_MONSTER_RVOL`, `VOLUME_MONSTER_MIN_MOVE_PCT`, `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`, `VOLUME_MONSTER_MAX_UNIVERSE`.
- **Schedule**: RTH (unless `VOLUME_MONSTER_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
🚨 VOLUME MONSTER — AXSM (12-30-2025 · 2:21 PM EST)
────────────
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410 (6.3x avg)
• 💰 Dollar Vol: $558,770,642
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

#### Gap Flow (Gap Up / Gap Down)
- **Purpose**: Detects opening gaps vs prior close with liquidity and RVOL filters.
- **Env**: `GAP_FLOW_MAX_UNIVERSE` (uses global min price/volume/RVOL + gap % logic in code).
- **Schedule**: RTH (unless `GAP_FLOW_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
🚀 GAP FLOW — AXSM (12-30-2025 · 9:45 AM EST)
────────────
• Direction: Gap Up (🔼 +6.5% vs prior close)
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410
• 💰 Dollar Vol: $484,885,891
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```
(Gap Down uses 🔻/🔻 and negative %.)

#### Swing Pullback
- **Purpose**: Dip-buy setups inside strong uptrends near moving averages.
- **Env**: `SWING_MIN_PRICE`, `SWING_MIN_TREND_DAYS`, `SWING_MIN_PULLBACK_PCT`, `SWING_MAX_PULLBACK_PCT`, `SWING_MIN_RVOL`, `SWING_MIN_DOLLAR_VOL`, `SWING_PULLBACK_MAX_UNIVERSE`.
- **Schedule**: RTH (unless `SWING_PULLBACK_ALLOW_OUTSIDE_RTH=true`).
- **Alert format (current send_alert header + body text)**:
```text
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

#### Trend Rider
- **Purpose**: Breakouts in strong uptrends (price above MAs, new highs, healthy move).
- **Env**: `TREND_RIDER_MIN_PRICE`, `TREND_RIDER_MIN_DOLLAR_VOL`, `TREND_RIDER_MIN_RVOL`, `TREND_RIDER_TREND_DAYS`, `TREND_RIDER_MIN_BREAKOUT_PCT`, `TREND_RIDER_MIN_MOVE_PCT`, `TREND_RIDER_MAX_UNIVERSE`.
- **Schedule**: RTH (unless `TREND_RIDER_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
🧠 TREND_RIDER — NVDA
💰 Last: $522.30
📊 RVOL: 2.1x
────────────
• Last: $522.30 (+3.4% today)
• Breakout: new 20-day high (510.00)
• Trend: MA20 500.10 > MA50 475.25
• Volume: 2.1× avg — Dollar Vol $5,100,000,000
• Link: https://www.tradingview.com/chart/?symbol=NVDA
```

#### Panic Flush
- **Purpose**: Capitulation-style down days pressing near lows with heavy RVOL.
- **Env**: `PANIC_FLUSH_MIN_PRICE`, `PANIC_FLUSH_MIN_DOLLAR_VOL`, `PANIC_FLUSH_MIN_RVOL`, `PANIC_FLUSH_MIN_DAY_DROP_PCT` (or `PANIC_FLUSH_MIN_DROP`), `PANIC_FLUSH_MAX_FROM_LOW_PCT`, `PANIC_FLUSH_MIN_FROM_OPEN_PCT`.
- **Schedule**: RTH (unless `PANIC_FLUSH_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
🧠 PANIC_FLUSH — AFRM
💰 Last: $18.42
📊 RVOL: 2.8x
────────────
• Last: $18.42 (-9.8% vs prior close, -6.2% from open)
• Volume: 12,300,000 (2.8× avg) — Dollar Vol: $226,260,000
• Near low: 1.2% off LOD
• Context: Capitulation-style selloff with heavy volume.
• Chart: https://www.tradingview.com/chart/?symbol=AFRM
```

#### Momentum Reversal
- **Purpose**: Large intraday move that starts to reverse (bullish or bearish).
- **Env**: `MOMO_REV_MIN_PRICE`, `MOMO_REV_MIN_DOLLAR_VOL`, `MOMO_REV_MIN_RVOL`, `MOMO_REV_MIN_MOVE_PCT`, `MOMO_REV_MIN_RECLAIM_PCT`, `MOMO_REV_MAX_FROM_EXTREME_PCT`.
- **Schedule**: RTH (unless `MOMENTUM_REVERSAL_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
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
(Bearish variant uses header text `MOMENTUM_REVERSAL BEARISH`.)

#### RSI Signals
- **Purpose**: Overbought/oversold signals on configurable timeframe.
- **Env**: `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `RSI_MIN_PRICE`, `RSI_MIN_DOLLAR_VOL`, `RSI_MAX_UNIVERSE`.
- **Schedule**: RTH (unless `RSI_ALLOW_OUTSIDE_RTH=true`).
- **Alert examples**:
```text
🧠 RSI_OVERSOLD — AMD
💰 Last: $102.40
────────────
🟢 RSI OVERSOLD — AMD
🕒 10:32 AM EST · Jan 01
────────────
📊 RSI: 27.4 (prev 30.1)
💰 Price: $102.40
💵 Intraday dollar volume (approx): $850,000,000
⏱ Timeframe: 5-min
🔗 Chart: https://www.tradingview.com/chart/?symbol=AMD

Potential oversold bounce / entry candidate. Combine with ORB, support, and options flow before acting.
```
```text
🧠 RSI_OVERBOUGHT — NVDA
💰 Last: $522.30
────────────
🔴 RSI OVERBOUGHT — NVDA
🕒 11:14 AM EST · Jan 01
────────────
📊 RSI: 74.2 (prev 72.8)
💰 Price: $522.30
💵 Intraday dollar volume (approx): $5,400,000,000
⏱ Timeframe: 5-min
🔗 Chart: https://www.tradingview.com/chart/?symbol=NVDA

Potential overbought fade / take-profit / short candidate. Combine with ORB, resistance, and options flow.
```

#### Opening Range Breakout (ORB)
- **Purpose**: Breakout/breakdown with retests of the opening range.
- **Env**: `ORB_RANGE_MINUTES`, `ORB_MIN_PRICE`, `ORB_MIN_DOLLAR_VOL`, `ORB_MIN_RVOL`, `ORB_RETEST_TOLERANCE_PCT`, `ORB_MAX_UNIVERSE`.
- **Schedule**: RTH post-opening-range window.
- **Alert examples**:
```text
🧠 ORB_LONG — AMD
💰 Last: $102.40
📊 RVOL: 1.8x
────────────
🚀 ORB LONG (breakout + retest) — AMD
🕒 10:00 AM EST · Jan 01
────────────
💰 Price: $102.40
📊 RVOL: 1.80
💵 Intraday $Vol: ≈ $750,000,000

📈 ORB High: $101.50
📉 ORB Low:  $99.80
🔁 Retest band: around $101.50

Bias: LONG idea above ORB high after retest. Combine with RSI + options flow for entries.
🔗 Chart: https://www.tradingview.com/chart/?symbol=AMD
```
```text
🧠 ORB_SHORT — MU
💰 Last: $78.10
📊 RVOL: 1.6x
────────────
🔻 ORB SHORT (breakdown + retest) — MU
🕒 10:05 AM EST · Jan 01
────────────
💰 Price: $78.10
📊 RVOL: 1.60
💵 Intraday $Vol: ≈ $520,000,000

📉 ORB Low:  $77.50
📈 ORB High: $79.30
🔁 Retest band: around $77.50

Bias: SHORT / take-profit idea below ORB low after retest. Combine with RSI + options flow for timing.
🔗 Chart: https://www.tradingview.com/chart/?symbol=MU
```

#### Squeeze
- **Purpose**: Short-squeeze style moves (big up move, high RVOL, near HOD).
- **Env**: `SQUEEZE_MIN_PRICE`, `SQUEEZE_MIN_DAY_MOVE_PCT`, `SQUEEZE_MIN_INTRADAY_FROM_OPEN_PCT`, `SQUEEZE_MIN_RVOL_EQUITY`, `SQUEEZE_MIN_DOLLAR_VOL`, `SQUEEZE_MAX_FROM_HIGH_PCT`, `SQUEEZE_MAX_UNIVERSE`.
- **Schedule**: RTH (unless `SQUEEZE_ALLOW_OUTSIDE_RTH=true`).
- **Alert format**:
```text
🧠 SQUEEZE — GME
💰 Last: $32.40
📊 RVOL: 3.5x
────────────
SQUEEZE RADAR — GME
• Last: $32.40 (+12.4% vs close, +9.1% from open)
• Volume: 18,500,000 (3.5× avg) — Dollar Vol: $599,000,000
• Near HOD: 1.2% off high
• Context: Strong up move with heavy volume; potential squeeze continuation.
• Chart: https://www.tradingview.com/chart/?symbol=GME
```

#### Dark Pool Radar
- **Purpose**: Highlights unusual dark-pool activity (aggregate and largest prints).
- **Env**: uses shared MIN_VOLUME_GLOBAL / RVOL and internal dark-pool thresholds.
- **Schedule**: RTH dark-pool window defined in code.
- **Alert format**:
```text
🧠 DARK_POOL — MSFT
💰 Last: $375.20
📊 RVOL: 1.4x
────────────
🕒 02:15 PM EST · Jan 01
📡 Dark pool prints (last 30 min)
────────────
📦 Prints: 42
💰 Total Notional: ≈ $310,000,000
🏦 Largest Print: ≈ $58,000,000
📊 Day Move: 1.8% · Dollar Vol: ≈ $8,200,000,000
🔗 Chart: https://www.tradingview.com/chart/?symbol=MSFT
```

#### Earnings
- **Purpose**: Flags stocks with earnings-driven moves (gap + intraday momentum).
- **Env**: `EARNINGS_MAX_FORWARD_DAYS`, global price/volume floors.
- **Schedule**: RTH.
- **Alert format**:
```text
🧠 EARNINGS — AAPL
💰 Last: $191.40
📊 RVOL: 2.3x
────────────
💎 EARNINGS MOVE — AAPL
🕒 10:12 AM EST · Jan 01
💰 Price: $191.40
📊 Move: 5.8% · Gap: 4.1% · Intraday: 1.6%
📦 Vol: 22,500,000 (≈ $4,304,000,000) · RVOL: 2.3x
🎯 Grade: A-
📰 Earnings: After Close (confirmed)
🔗 Chart: https://www.tradingview.com/chart/?symbol=AAPL
```

#### Daily Ideas
- **Purpose**: Twice-per-day confluence scan (trend + VWAP + RVOL + RSI + options flow bias) that ranks top long/short ideas.
- **Env**: `DAILY_IDEAS_ALLOW_OUTSIDE_WINDOW` (optional), uses shared universe + RSI/flow helpers.
- **Schedule**: AM window ~10:45–11:00 ET, PM window ~15:15–15:30 ET (one run per slot per day).
- **Alert examples**:
```text
💡 DAILY IDEAS — LONGS (01-01-2026 · 10:52 AM EST)
────────────
Top 5 LONG ideas (ranked by confluence score):

NVDA — Score: 9.1 / 10
   Trend: strong_up (2.5%)
   💵 Price: $522.30 (O: $510.00, H: $525.40, L: $508.20)
   📊 Intraday: +2.4% vs prior close, above VWAP | RVOL 2.5x
   🔍 RSI (5m): 54.2
   🧩 Options flow bias: +0.72
   📈 Chart: https://www.tradingview.com/chart/?symbol=NVDA
```
```text
💡 DAILY IDEAS — SHORTS (01-01-2026 · 03:20 PM EST)
────────────
Top 5 SHORT ideas (ranked by confluence score):

AAPL — Score: 8.8 / 10
   Trend: downtrend (-2.1%)
   💵 Price: $191.40 (O: $195.10, H: $196.00, L: $190.50)
   📊 Intraday: -2.1% vs prior close, below VWAP | RVOL 1.9x
   🔍 RSI (5m): 73.5
   🧩 Options flow bias: -0.66
   📈 Chart: https://www.tradingview.com/chart/?symbol=AAPL
```
(If no ideas, alerts explicitly state “No high-confluence LONG/SHORT ideas for this slot.”)

#### Premarket
- **Purpose**: Premarket gappers with liquidity and RVOL filters.
- **Env**: `MIN_PREMARKET_PRICE`, `MIN_PREMARKET_MOVE_PCT`, `MAX_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, `PREMARKET_TICKER_UNIVERSE`, `PREMARKET_ALLOW_OUTSIDE_WINDOW`.
- **Schedule**: Premarket 04:00–09:29 ET (unless override).
- **Alert format**:
```text
🧠 PREMARKET — NFLX
💰 Last: $410.20
📊 RVOL: 1.9x
────────────
📣 PREMARKET — NFLX
🕒 08:15 AM EST · Jan 01
💰 $410.20 · 📊 RVOL 1.9x
────────────
📈 Prev Close: $395.00 → Premarket Last: $410.20
📊 Premarket Range: $404.00 – $412.50
📦 Premarket Vol: 1,250,000 (≈ $512,750,000)
💰 Day Vol (partial): 1,800,000 (≈ $738,000,000)
📊 RVOL (partial): 1.9x
🎯 Grade: A
🧠 Bias: Long gap continuation
🔗 Chart: https://www.tradingview.com/chart/?symbol=NFLX
```

### Options Bots
All option flow bots use shared parsing/formatting from `options_common.py` (human-readable contracts, MM-DD-YYYY expiries, DTE, premium/size/notional, IV/volume/OI) and filter trades to the current trading day.

#### Options Cheap Flow (💰)
- **Purpose**: Low-premium contracts with meaningful size/notional.
- **Env**: `CHEAP_MAX_PREMIUM`, `CHEAP_MIN_NOTIONAL`, `CHEAP_MIN_SIZE`, `OPTIONS_MIN_UNDERLYING_PRICE`, `OPTIONS_FLOW_MAX_UNIVERSE`, optional DTE bounds.
- **Schedule**: RTH (unless `OPTIONS_CHEAP_FLOW_ALLOW_OUTSIDE_RTH=true`).
- **Alert example**:
```text
💰 CHEAP FLOW — QID (12-30-2025 · 2:21 PM EST)
────────────
• Contract: QID 9C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $16.20
• 💰 Premium: $0.35 | Size: 100 | Notional: $3,500
• 📊 IV: 41.7% | Volume: 800 | OI: 1,900
• 📈 Chart: https://www.tradingview.com/chart/?symbol=QID
```

#### Options Unusual Flow (⚠️)
- **Purpose**: Notable size/notional flows (not necessarily whale-sized) within DTE constraints.
- **Env**: `UNUSUAL_MIN_NOTIONAL`, `UNUSUAL_MIN_SIZE`, `UNUSUAL_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`.
- **Alert example**:
```text
⚠️ UNUSUAL FLOW — TSLA (12-30-2025 · 2:21 PM EST)
────────────
• Contract: TSLA 250C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $242.10
• 💰 Premium: $4.20 | Size: 120 | Notional: $50,400
• 📊 IV: 58.3% | Volume: 2,300 | OI: 5,100
• 📈 Chart: https://www.tradingview.com/chart/?symbol=TSLA
```

#### Options Whales (🐳)
- **Purpose**: Very large, whale-sized positions (high notional/size, DTE-limited).
- **Env**: `WHALES_MIN_NOTIONAL`, `WHALES_MIN_SIZE`, `WHALES_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`.
- **Alert example**:
```text
🐳 WHALE FLOW — BDX (12-30-2025 · 2:21 PM EST)
────────────
• Contract: BDX 130C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $129.82
• 💰 Premium: $6.52 | Size: 100 | Notional: $652,000
• 📊 IV: 34.2% | Volume: 1,200 | OI: 3,400
• 📈 Chart: https://www.tradingview.com/chart/?symbol=BDX
```

#### Options IV Crush (🔥)
- **Purpose**: Contracts with sharp implied-volatility drops (often post-earnings).
- **Env**: `IVCRUSH_MIN_IV_DROP_PCT`, `IVCRUSH_MIN_VOL`, `IVCRUSH_MAX_DTE`, `OPTIONS_MIN_UNDERLYING_PRICE`.
- **Alert example**:
```text
🔥 IV CRUSH — AMD (12-30-2025 · 2:21 PM EST)
────────────
• Contract: AMD 110C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $112.40
• 💰 Premium: $2.15 | Size: 200 | Notional: $43,000
• 📊 IV: 68.0% → 48.0% (drop 20.0%) | Volume: 1,800 | OI: 4,200
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AMD
```

#### Options Indicator (analytics)
- **Purpose**: High-IV momentum vs low-IV reversal regimes with multi-factor context (IV rank, RSI, MACD, Bollinger bands, options OI).
- **Env**: Uses shared thresholds and underlying universe; runs in RTH.
- **Alert example**:
```text
🧠 OPTIONS_INDICATOR — SPY
💰 Last: $475.10
📊 RVOL: 1.3x
────────────
📈 OPTIONS INDICATOR — SPY
🕒 02:52 PM EST · Jan 01
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

🧠 Bias: Bullish momentum
🔗 Chart: https://www.tradingview.com/chart/?symbol=SPY
```

---
## 📢 Example Alerts
The above subsections show the exact Telegram formats emitted by each bot. Copy/paste samples illustrate headers, emojis, timestamps, OHLC/RVOL/dollar volume blocks, option contract formatting, and TradingView links as implemented in the codebase.

---
## Monitoring & Heartbeat
- **Status bot (`bots/status_report.py`)** reads `STATUS_STATS_PATH`, filters to today’s trading day (EST), and sends the “MoneySignalAI Heartbeat”:
  - Bots section: last run time or “No run today”.
  - Totals: sum of today’s scanned/matches/alerts.
  - Per-bot stats: today-only scanned | matches | alerts with thousands separators.
  - Diagnostics: high-scan zero-alert bots, ran-today-zero-scans, not-run-today.
  - Runtime: median + last runtime for today’s runs (or “no runtime data yet”).
- Errors surfaced via `record_error` appear under ERRORS DETECTED in the header.

---
## Extensibility & Roadmap
- **Add a new strategy** by creating `bots/<new_bot>.py` with `async def run_<new_bot>()`, registering it in `BOT_DEFS` (main.py), and reusing `resolve_universe_for_bot`, RTH/premarket gates, and `record_bot_stats`.
- **Tune behavior** by adjusting env thresholds (RVOL, dollar vol, DTE, IV drops, RSI bands) without code changes.
- **Future hooks**: multi-account routing, broker/execution integrations, web dashboards on top of the existing heartbeat/stats JSON.

MoneySignalAI is built to be observable, configurable, and production-ready for high-signal equities and options alerting.
