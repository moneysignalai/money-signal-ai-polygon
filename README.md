<p align="center">
  <img src="docs/moneysignal-logo.png" alt="MoneySignalAI Logo" width="320">
</p>

<h1 align="center">MoneySignalAI</h1>

<p align="center">
  Async FastAPI service that schedules multiple equity and options scanners and sends formatted alerts to Telegram.
</p>

---

## Overview

MoneySignalAI runs a suite of intraday scanners that monitor U.S. equities and options flow. A FastAPI app hosts health endpoints while an asyncio scheduler launches each bot on its own cadence. Market data is fetched through the shared helper layer, which can talk to Massive or Polygon depending on environment configuration. Alerts are pushed to Telegram with concise, emoji-friendly formatting.

Key traits:

- **Async bot runner:** `main.py` schedules each bot with per-bot intervals and timeouts.
- **Shared data layer:** `bots/shared.py` handles time utilities, universe resolution, caching, and market data access.
- **Telegram delivery:** Alerts and status pings use Telegram bots configured via environment variables.
- **Clear scheduling visibility:** Disabled or test-mode gating is logged so you can see why a bot did not run during a cycle.

## Active bots

The scheduler loads bots from the `bots/` directory (excluding the legacy `oldcode` folder). Each exposes a single async entrypoint used by `main.py`.

| Bot | Purpose | Cadence (default) |
| --- | --- | --- |
| `premarket` | Surface pre-market movers that meet dollar volume and RVOL floors. | 60s |
| `volume_monster` | Track extreme volume spikes with meaningful price swings. | 20s |
| `gap_scanner` | Surface gap-up and gap-down setups with liquidity filters. | 20s |
| `swing_pullback` | Find pullbacks within established uptrends. | 20s |
| `panic_flush` | Flag capitulation-style selloffs near lows with heavy volume. | 20s |
| `momentum_reversal` | Detect intraday reversals after strong directional moves. | 20s |
| `opening_range_breakout` | Identify opening-range breakouts with RVOL confirmation. | 20s |
| `options_flow` | Scan option chain snapshots and last trades for cheap, unusual, whale, and IV crush contracts. | 20s |
| `options_indicator` | Compute option-related indicators and alerts. | 60s |
| `squeeze` | Flag potential squeeze setups using option flow thresholds. | 60s |
| `rsi_signals` | Generate RSI-based signals across the dynamic universe. | 20s |
| `trend_flow` | Look for trend-aligned flow with relative-volume checks. | 60s |
| `earnings` | Surface upcoming earnings names with activity context. | 300s |
| `dark_pool_radar` | Summarize dark pool / ATS clusters. | 60s |
| `daily_ideas` | Produce slower-cadence daily idea summaries. | 600s |
| `status_report` | Track bot health, recent runtimes, and error counts. | scheduled internally |
| `debug_ping` / `debug_status_ping` | Optional liveness pings controlled by env flags. | on-demand |

## Environment configuration

Environment variables are the source of truth for configuration. Key settings include:

- **Data providers:**
  - `POLYGON_KEY` (or `POLYGON_API_KEY`): API token used for Massive/Polygon-compatible endpoints.
  - `POLYGON_BASE_URL` (optional): override base URL (e.g., `https://api.massive.com`). Defaults to `https://api.polygon.io`.
- **Scheduling:**
  - `SCAN_INTERVAL_SECONDS`: base scheduler tick (default 20s).
  - `BOT_TIMEOUT_SECONDS`: per-bot timeout guard.
  - `<BOTNAME>_INTERVAL`: per-bot override (e.g., `OPTIONS_FLOW_INTERVAL`).
  - `DISABLED_BOTS`, `TEST_MODE_BOTS`: comma-separated lists to disable or mark bots as test-only.
- **Telegram:**
  - `TELEGRAM_TOKEN_ALERTS`, `TELEGRAM_CHAT_ALL`: required for alert delivery.
  - `TELEGRAM_TOKEN_STATUS`: optional status channel token.
- **Global floors:**
  - `MIN_RVOL_GLOBAL`, `MIN_VOLUME_GLOBAL`: shared volume filters for universe selection.
- **Options flow tuning (examples):**
  - `OPTIONS_FLOW_TICKER_UNIVERSE`, `OPTIONS_FLOW_MAX_UNIVERSE`, `OPTIONS_FLOW_ALLOW_OUTSIDE_RTH`
  - `CHEAP_MAX_PREMIUM`, `CHEAP_MIN_SIZE`, `CHEAP_MIN_NOTIONAL`
  - `UNUSUAL_MAX_DTE`, `UNUSUAL_MIN_SIZE`, `UNUSUAL_MIN_NOTIONAL`
  - `WHALES_MAX_DTE`, `WHALES_MIN_SIZE`, `WHALES_MIN_NOTIONAL`
  - `IVCRUSH_MAX_DTE`, `IVCRUSH_MIN_IV_DROP_PCT`, `IVCRUSH_MIN_VOL`

Refer to `.env` or Render dashboard settings for the full list of supported variables used across bots.

## Running locally

1. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables**

   Export the required keys (API token and Telegram) plus any bot overrides you need. A `.env` file loaded by your process manager also works.

3. **Start the service**

   ```bash
   uvicorn main:app --reload --host 0.0.0.0 --port 8000
   ```

   The scheduler runs as a background task inside the FastAPI app. Health is available at `/health`, and the root endpoint returns current configuration and bot registry details.

## Project layout

```
main.py              # FastAPI app + asyncio scheduler
bots/
  shared.py          # Shared helpers (time, universe, data access, Telegram, caching)
  options_flow.py    # Options flow scanner (cheap / unusual / whale / IV crush)
  ...                # Additional bots listed above
```

Legacy strategies live in `bots/    oldcode  /` and are not scheduled by default.

## Notes for contributors

- Keep bot entrypoints consistent: each module should expose an async `run_bot()` or the named function referenced in `main.py`.
- Use the shared provider utilities in `bots/shared.py` for all market data access (including Massive/Polygon option snapshots and last trades).
- Respect environment-based gates like regular trading hours and DISABLED_BOTS so the scheduler remains stable.

