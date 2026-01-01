# MoneySignalAI

MoneySignalAI is a production-grade, multi-strategy equities and options scanner that streams human-readable trade ideas to Telegram in real time. A FastAPI-based scheduler orchestrates dozens of independent bots in parallel, scans a dynamic top-volume universe of up to ~1,500 tickers (with static fallbacks), and publishes a rich heartbeat so operators can monitor health, throughput, and latency.【F:main.py†L26-L114】【F:bots/shared.py†L620-L636】

## Key Features
- **Modular bot catalog** covering intraday equities, swing/trend, dark pool, earnings, daily confluence, and four dedicated options-flow scanners, each with its own thresholds and alert formatting.【F:main.py†L54-L80】
- **Dynamic universe selection**: pulls the most liquid names via the data provider and caps scans to 1,500 tickers, falling back to `TICKER_UNIVERSE` or an emergency set when needed.【F:bots/shared.py†L639-L709】【F:bots/shared.py†L620-L636】
- **Configurable via environment**: RVOL, dollar-volume floors, IV-crush %, gap thresholds, scan intervals, timeouts, Telegram routing, and more are all controlled with env vars (see below).【F:bots/shared.py†L19-L57】【F:main.py†L26-L49】
- **Production telemetry**: every run records scanned/matched/alert counts, runtime, and trading-day tags for a daily heartbeat that highlights “high scan, zero alert” scenarios and skipped bots.【F:bots/shared.py†L329-L395】【F:bots/status_report.py†L33-L200】
- **Human-friendly alerts**: standardized emojis, Eastern timestamps, TradingView chart links, readable options contracts (MM-DD-YYYY expiries), and consistent field ordering across bots.【F:bots/gap_flow.py†L90-L121】【F:bots/options_common.py†L188-L218】

---

## Bots & Alert Logic
Below is the active catalog as wired in `main.py`. Each bot exposes an async `run_*` entrypoint and records stats through the shared helpers.

### Volume Monster
Intraday “monster bar” detector focusing on outsized RVOL and dollar volume. It scans the dynamic universe, checks daily bars for RVOL/dollar-volume spikes, and alerts with OHLC, RVOL, and dollar volume lines.【F:main.py†L54-L70】【F:bots/volume_monster.py†L83-L109】

**Key envs:** `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `VOLUME_MONSTER_RVOL`, `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`, `VOLUME_MONSTER_MAX_UNIVERSE`.

**Sample alert:**
```
🚨 VOLUME MONSTER — AXSM (12-30-2025 · 2:21 PM EST)
────────────
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410 (6.3x avg)
• 💰 Dollar Vol: $558,770,642
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

### Gap Flow (Gap Up / Gap Down)
Detects strong gaps versus the prior close with liquidity/RVOL checks. Alerts highlight direction, gap %, OHLC, RVOL, and dollar volume with a timestamped header and TradingView link.【F:bots/gap_flow.py†L90-L121】

**Key envs:** `GAP_FLOW_MAX_UNIVERSE`, `MIN_PREMARKET_MOVE_PCT`, `MIN_PREMARKET_DOLLAR_VOL`, `MIN_PREMARKET_RVOL`, global volume floors.

**Sample alert:**
```
🚀 GAP FLOW — AXSM (12-30-2025 · 9:45 AM EST)
────────────
• Direction: Gap Up (🔼 +6.5% vs prior close)
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410
• 💰 Dollar Vol: $484,885,891
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

### Swing Pullback
Dip-buy scanner inside strong uptrends. Uses moving-average trend checks and RVOL/volume floors to surface orderly pullbacks with continuation potential. See `bots/swing_pullback.py` for scoring and alert text.

**Key envs:** `SWING_MIN_TREND_DAYS`, `SWING_MIN_PULLBACK_PCT`, `SWING_MAX_PULLBACK_PCT`, `SWING_MIN_PRICE`, `SWING_MIN_RVOL`, `TREND_RIDER_MIN_DOLLAR_VOL`.

### Trend Rider
Trend-following breakout bot for established uptrends. Requires price above key MAs, fresh highs over a breakout lookback, and liquidity floors before alerting.【F:bots/trend_rider.py†L1-L120】

**Key envs:** `TREND_RIDER_MIN_DOLLAR_VOL`, `TREND_RIDER_MIN_RVOL`, `TREND_RIDER_TREND_DAYS`, `TREND_RIDER_MIN_BREAKOUT_PCT`, `TREND_RIDER_MIN_PRICE`.

### Panic Flush
Captures capitulation-style down moves near lows with elevated RVOL and dollar volume; alerts show depth of drop, proximity to lows, RVOL, and notional context.【F:bots/panic_flush.py†L1-L160】

**Key envs:** `PANIC_FLUSH_MIN_DROP`, `PANIC_FLUSH_MIN_RVOL`, `PANIC_FLUSH_MIN_DOLLAR_VOL`, global floors.

### Momentum Reversal
Mean-reversion style intraday scanner that looks for large initial moves reversing toward VWAP with sufficient RVOL; alerts highlight reclaim magnitude and liquidity context.【F:bots/momentum_reversal.py†L1-L150】

**Key envs:** `MOMO_REV_MIN_RECLAIM_PCT`, `MOMO_REV_MIN_RVOL`, `MOMO_REV_MIN_MOVE_PCT`, `MOMO_REV_MIN_DOLLAR_VOL`, `MOMO_REV_MIN_PRICE`.

### RSI Signals
Screens the universe for overbought/oversold conditions on intraday intervals using the shared RSI helper and minimum price/liquidity filters.【F:main.py†L61-L63】【F:bots/rsi_signals.py†L1-L120】

**Key envs:** `RSI_PERIOD`, `RSI_TIMEFRAME_MIN`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`, `RSI_MIN_PRICE`, `RSI_MIN_DOLLAR_VOL`.

### Opening Range Breakout (ORB)
Defines the opening range over `ORB_RANGE_MINUTES` and alerts on breaks with RVOL and dollar-volume floors.【F:main.py†L63-L68】【F:bots/openingrangebreakout.py†L1-L120】

**Key envs:** `ORB_RANGE_MINUTES`, `ORB_MIN_DOLLAR_VOL`, `ORB_MIN_RVOL`.

### Squeeze
“Stock short-squeeze style bot (price + volume only)” that looks for large up days with strong RVOL, healthy dollar volume, and closes near highs. Alerts show price move, RVOL, notional, and chart link.【F:bots/squeeze.py†L1-L120】

**Key envs:** `SQUEEZE_MIN_PREMIUM`, `SQUEEZE_MIN_NOTIONAL`, `SQUEEZE_MIN_SIZE`, `SQUEEZE_MAX_UNIVERSE`, global volume floors.

### Dark Pool Radar
Surfaces notable dark-pool/block activity with notional and volume context using the shared data client and the same stats pipeline as other bots.【F:bots/dark_pool_radar.py†L1-L80】

### Earnings
Alerts on near-term earnings using provider calendars and `EARNINGS_MAX_FORWARD_DAYS`, reporting symbols, dates, and context for upcoming catalysts.【F:bots/earnings.py†L1-L80】

### Daily Ideas
Twice-daily confluence bot (AM and PM slots) that blends trend, VWAP, RVOL, 5‑minute RSI, and options-flow bias to rank top long/short ideas. Sends ranked lists (or “no ideas” summaries) and records scans/matches per slot.【F:bots/daily_ideas.py†L1-L200】

### Options Flow Family (shared parsing in `bots/options_common.py`)
All options bots filter current-session trades, parse OCC symbols into human-readable contracts, and emit standardized alerts with expiries in MM-DD-YYYY, premium/size/notional, IV, DTE, and chart links.【F:bots/options_common.py†L188-L218】

- **Options Cheap Flow**: flags low-premium contracts that still clear size/notional floors (`CHEAP_MAX_PREMIUM`, `CHEAP_MIN_NOTIONAL`, `CHEAP_MIN_SIZE`, `OPTIONS_MIN_UNDERLYING_PRICE`).
- **Options Unusual Flow**: finds notable size/notional prints within `UNUSUAL_MAX_DTE`, using `UNUSUAL_MIN_NOTIONAL` and `UNUSUAL_MIN_SIZE` thresholds.
- **Options Whales**: detects very large “whale” orders using `WHALES_MIN_NOTIONAL`, `WHALES_MIN_SIZE`, `WHALES_MAX_DTE` and highlights with 🐳 header emoji.【F:main.py†L69-L73】【F:bots/options_whales.py†L1-L140】
- **Options IV Crush**: surfaces contracts with sharp IV drops, using `IVCRUSH_MIN_IV_DROP_PCT`, `IVCRUSH_MIN_VOL`, `IVCRUSH_MAX_DTE`; alerts label with 🔥 and include IV change context.【F:bots/options_iv_crush.py†L1-L150】

**Sample options alert:**
```
🐳 WHALE FLOW — BDX (12-30-2025 · 2:21 PM EST)
────────────
• Contract: BDX 130C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $129.82
• 💰 Premium: $6.52 | Size: 100 | Notional: $652,000
• 📊 IV: 34.2% | Volume: 1,200 | OI: 3,400
• 📈 Chart: https://www.tradingview.com/chart/?symbol=BDX
```

### Premarket
Scans premarket gappers/volume leaders using premarket RVOL/dollar-volume/price floors, honoring premarket-only windows and per-bot universes (`PREMARKET_TICKER_UNIVERSE`).【F:main.py†L54-L56】【F:bots/premarket.py†L1-L120】

---

## Architecture & Project Layout
- **Scheduler / Runner (`main.py`)**: FastAPI app plus background scheduler that loads a registry of bots (public name, module path, entrypoint, interval). Applies `DISABLED_BOTS`/`TEST_MODE_BOTS`, time-of-day gating (RTH vs premarket), and runs bots with per-bot timeouts.【F:main.py†L26-L114】【F:main.py†L51-L81】
- **Shared utilities (`bots/shared.py`)**: time helpers (EST timestamps, RTH/premarket windows, trading-day checks), universe resolution with dynamic top-volume fallback and 1,500 cap, HTTP helpers with retries, Telegram wrappers, and unified stats writer with trading-day scoping.【F:bots/shared.py†L58-L109】【F:bots/shared.py†L620-L709】【F:bots/shared.py†L329-L395】
- **Options helpers (`bots/options_common.py`)**: OCC parsing, DTE computation, IV extraction, trade timestamp filtering to today’s session, and shared alert formatter for all option bots.【F:bots/options_common.py†L55-L218】
- **Bots (`bots/*.py`)**: each strategy file exposes an async `run_*` entrypoint and calls shared utilities for universes, telemetry, and Telegram delivery (examples: `bots/gap_flow.py`, `bots/volume_monster.py`).【F:main.py†L54-L81】【F:bots/gap_flow.py†L124-L151】
- **Status / Heartbeat (`bots/status_report.py`)**: reads/writes stats JSON, aggregates today-only runs, prints per-bot status, diagnostics, and runtimes, and sends the MoneySignalAI Heartbeat to Telegram.【F:bots/status_report.py†L33-L200】
- **Scripts**: `scripts/smoke_test.py` for quick import/run checks.

---

## Getting Started

### Prerequisites
- Python 3.10+ (per project usage and dependency set).
- Data provider key via `POLYGON_KEY` (Massive-compatible endpoints supported).【F:bots/shared.py†L16-L18】
- Telegram configuration: `TELEGRAM_TOKEN_ALERTS`, `TELEGRAM_TOKEN_STATUS` (optional), and `TELEGRAM_CHAT_ALL` chat ID.【F:bots/shared.py†L23-L33】

### Clone & Install
```bash
git clone https://github.com/your-org/money-signal-ai-polygon.git
cd money-signal-ai-polygon
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Configuration (.env)
Set environment variables before running. Key groups include:
- **Universe & dynamics**: `TICKER_UNIVERSE`, `FALLBACK_TICKER_UNIVERSE`, `DYNAMIC_MAX_TICKERS` (capped to 1500), `DYNAMIC_VOLUME_COVERAGE`.【F:bots/shared.py†L620-L709】
- **Global floors**: `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`, premarket floors (`MIN_PREMARKET_*`).【F:bots/shared.py†L19-L22】【F:bots/gap_flow.py†L134-L141】
- **Bot tuning**: per-strategy thresholds such as `VOLUME_MONSTER_MIN_DOLLAR_VOL`, `PANIC_FLUSH_MIN_DROP`, `TREND_RIDER_MIN_RVOL`, `RSI_*`, `SQUEEZE_*`, `EARNINGS_MAX_FORWARD_DAYS`, `OPTIONS_*` families (CHEAP/UNUSUAL/WHALES/IVCRUSH), etc.【F:main.py†L54-L81】【F:bots/options_whales.py†L1-L140】
- **Operations**: `SCAN_INTERVAL_SECONDS`, `BOT_TIMEOUT_SECONDS`, `STATUS_HEARTBEAT_INTERVAL_MIN`, `DEBUG_FLOW_REASONS`, `DEBUG_PING_ENABLED`, `DEBUG_STATUS_PING_ENABLED`, `STATUS_STATS_PATH`, `OPTIONS_IV_CACHE_PATH`.【F:main.py†L26-L31】【F:bots/shared.py†L296-L323】

### Running locally
- FastAPI + scheduler: `uvicorn main:app --reload` or simply `python main.py` to launch the scheduler and health endpoints.【F:main.py†L90-L120】
- Bots run on their configured intervals; heartbeat posts every `STATUS_HEARTBEAT_INTERVAL_MIN` minutes via `status_report`.

### Deploying
- The repo includes `render.yaml` for Render deployments; set env vars in the dashboard and point the service at `main:app`.
- Containerized deployments can reuse the same entrypoint; ensure secrets (`POLYGON_KEY`, Telegram tokens) are provided.

---

## Example Alerts
Below are representative alert payloads as delivered to Telegram.

**Gap Flow**
```
🚀 GAP FLOW — AXSM (12-30-2025 · 9:45 AM EST)
────────────
• Direction: Gap Up (🔼 +6.5% vs prior close)
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410
• 💰 Dollar Vol: $484,885,891
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

**Volume Monster**
```
🚨 VOLUME MONSTER — AXSM (12-30-2025 · 2:21 PM EST)
────────────
• 💵 Last: $182.64 (O: $158.49, H: $184.40, L: $158.49)
• 📊 RVOL: 6.3x | Volume: 3,059,410 (6.3x avg)
• 💰 Dollar Vol: $558,770,642
• 📈 Chart: https://www.tradingview.com/chart/?symbol=AXSM
```

**Options Whale Flow**
```
🐳 WHALE FLOW — BDX (12-30-2025 · 2:21 PM EST)
────────────
• Contract: BDX 130C 01-16-2026 (⏳ 15 DTE)
• 💵 Underlying: $129.82
• 💰 Premium: $6.52 | Size: 100 | Notional: $652,000
• 📊 IV: 34.2% | Volume: 1,200 | OI: 3,400
• 📈 Chart: https://www.tradingview.com/chart/?symbol=BDX
```

**Daily Ideas (LONGS example)**
```
💡 DAILY IDEAS — LONGS (01-01-2026 · 10:52 AM EST)
────────────
Top 5 LONG ideas (ranked by confluence score):

NVDA — Score: 9.1 / 10
• Trend: strong uptrend (price above 20/50 MA)
• 💵 Price: $522.30 (O: $510.00, H: $525.40, L: $508.20)
• 📊 Intraday: +2.4% vs prior close, above VWAP
• 🔍 RSI (5m): 54.2
• 🧩 Options flow bias: heavy calls (flow_bias +0.72)
• 📈 Chart: https://www.tradingview.com/chart/?symbol=NVDA
```

---

## Monitoring & Heartbeat
`status_report.py` aggregates per-bot runs for the current trading day (EST), computes totals (scanned/matches/alerts), classifies bots as “No run today”, “Ran today, zero scans”, or “High scan, zero alerts”, and summarizes runtimes (median/last/n). It publishes the MoneySignalAI Heartbeat to Telegram on the configured interval.【F:bots/status_report.py†L33-L200】

Use this to:
- Confirm every enabled bot is running today and writing stats.
- Spot filter tuning issues (high scan, zero alerts) and empty-universe problems (ran today, zero scans).
- Track performance regressions via runtime medians.

---

## Extensibility & Roadmap
- **Add a new strategy** by creating a `bots/<name>.py` with an async `run_<name>()`, using `resolve_universe_for_bot`, and registering it in `main.py`’s registry.【F:main.py†L51-L88】【F:bots/shared.py†L639-L709】
- **Tune without code changes**: adjust env thresholds (RVOL, notional, DTE, universes) to modulate signal density and latency.
- **Future enhancements**: broker integrations for auto-execution, multi-account routing, or a web dashboard on top of the existing stats JSON.

MoneySignalAI is designed to be investor-grade: modular, observable, and ready to scale with new data sources or strategies while keeping operators informed through rich, human-readable alerts and heartbeat telemetry.【F:main.py†L90-L120】【F:bots/shared.py†L329-L395】
