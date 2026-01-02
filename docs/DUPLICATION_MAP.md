# Duplication Map

This map captures strategy overlap and the plan to keep alerts unique.

- **Gap Flow vs Premarket**
  - Overlap: Both watch premarket gappers and intraday follow-through.
  - Action: Keep both. Premarket focuses on early prints; Gap Flow enforces VWAP/hold tests after open. Share gap/universe helpers via `bots.shared` and align fixtures.

- **Volume Monster vs Trend Flow / Intraday Flow**
  - Overlap: Volume-driven momentum scans.
  - Action: Merge shared volume spike helpers (RVOL, dollar volume) into `bots.shared` and keep distinct thresholds: Volume Monster uses broad RVOL spikes; Trend/Intraday Flow keep trend-continuation filters and VWAP alignment.

- **Panic Flush vs Momentum Reversal**
  - Overlap: Both watch sharp moves with potential reversals.
  - Action: Keep both but differentiate: Panic Flush remains downside capitulation with VWAP distance; Momentum Reversal requires reclaim/engulf behavior. Include strategy tags in alerts.

- **Options Cheap Flow vs Options Unusual Flow**
  - Overlap: Both read large option trades.
  - Action: Keep both with differentiated rules: Cheap Flow highlights low premium, smaller notional sweeps; Unusual Flow enforces high size/notional relative to OI. Share parsing via `options_common.iter_option_contracts`.

- **Options Unusual Flow vs Options Whales**
  - Overlap: Large options prints.
  - Action: Keep both. Whales raises notional + multi-sweep/OTM thresholds; Unusual Flow keeps mid-tier unusual activity. Enforce unique `strategy_tag` and alert "why fired" text.

- **Trend Rider vs Trend Flow**
  - Overlap: Trend continuation.
  - Action: Keep both. Trend Rider stays price-action focused; Trend Flow leans on flow prints + VWAP. Consolidate shared trend helpers.

- **Squeeze vs Volume Monster**
  - Overlap: Breakouts with RVOL.
  - Action: Keep both. Squeeze requires volatility compression then release; Volume Monster is raw flow spike. Add helper for squeeze state.

- **Dark Pool Radar vs Equity Flow**
  - Overlap: Large trade prints.
  - Action: Keep both. Dark Pool Radar restricted to off-exchange prints with ADV sizing; Equity Flow is lit tape/vwap alignment. Shared notional formatting helper.

- **Earnings vs Daily Ideas**
  - Overlap: Upcoming catalysts.
  - Action: Keep both. Earnings is calendar-driven alerts; Daily Ideas compiles setups. Share fixture ingestion in TEST_MODE.

## Recommended refactors
- Centralize universe resolution, request limiting, and option trade parsing in `bots.shared` / `bots.options_common` to avoid copy/paste.
- Add `strategy_tag` per bot and enforce unique alert titles + why-fired summaries.
- Add debug reason aggregation to avoid per-contract spam.
- Use scheduler guardrails to prevent overlapping runs and enforce cooldowns.
