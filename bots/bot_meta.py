"""Central registry for bot metadata and strategy tags."""

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class BotMeta:
    strategy_tag: str
    title_template: str
    why_template: str


BOT_METADATA: Dict[str, BotMeta] = {
    "premarket": BotMeta("GAP_PREMARKET", "Premarket Gap Alert", "Premarket gap with RVOL and dollar volume filter."),
    "volume_monster": BotMeta("VOLUME_MONSTER", "Volume Monster", "Intraday volume spike with RVOL confirmation."),
    "gap_flow": BotMeta("GAP_FLOW", "Gap Flow", "Gap holding VWAP / gap range with continuation."),
    "swing_pullback": BotMeta("SWING_PULLBACK", "Swing Pullback", "Pullback in prevailing trend with volume support."),
    "panic_flush": BotMeta("PANIC_FLUSH", "Panic Flush", "Downside flush with capitulation volume and VWAP distance."),
    "momentum_reversal": BotMeta("MOMO_REVERSAL", "Momentum Reversal", "Mean reversion via reclaim/engulf after expansion."),
    "trend_rider": BotMeta("TREND_RIDER", "Trend Rider", "Higher-high/low continuation with strength filters."),
    "rsi_signals": BotMeta("RSI_SIGNAL", "RSI Signal", "RSI extreme with supporting trend + volume filters."),
    "opening_range_breakout": BotMeta("OPEN_RANGE", "Opening Range Breakout", "Breakout of opening range with RVOL guard."),
    "options_cheap_flow": BotMeta("CHEAP_FLOW", "Cheap Options Flow", "Low-premium sweep with size vs OI filters."),
    "options_unusual_flow": BotMeta("UNUSUAL_FLOW", "Unusual Options Flow", "Large size/notional sweep vs baseline activity."),
    "options_whales": BotMeta("WHALE_FLOW", "Whale Options Flow", "Very high notional/OTM whale activity."),
    "options_iv_crush": BotMeta("IV_CRUSH", "IV Crush", "Sharp implied volatility compression post-event."),
    "options_indicator": BotMeta("IV_INDICATOR", "Options Indicator", "Greeks/IV percentile shift triggers."),
    "squeeze": BotMeta("SQUEEZE_BREAK", "Squeeze Break", "Volatility compression breakout with RVOL."),
    "dark_pool_radar": BotMeta("DARK_POOL", "Dark Pool Radar", "Large off-exchange prints vs ADV/VWAP."),
    "earnings": BotMeta("EARNINGS_CAL", "Earnings Calendar", "Upcoming earnings within configured window."),
    "daily_ideas": BotMeta("DAILY_IDEAS", "Daily Ideas", "Curated idea list from intraday signals."),
    "status_report": BotMeta("STATUS_HEARTBEAT", "Status Report", "Heartbeat summarizing bot health."),
    "equity_flow": BotMeta("EQUITY_FLOW", "Equity Flow", "Lit tape momentum / VWAP aligned."),
    "intraday_flow": BotMeta("INTRADAY_FLOW", "Intraday Flow", "High-frequency intraday flow pressure."),
    "trend_flow": BotMeta("TREND_FLOW", "Trend Flow", "Flow-supported trend continuation."),
    "gap_scanner": BotMeta("GAP_SCANNER", "Gap Scanner", "Premarket/daily gap monitor."),
    "openingrangebreakout": BotMeta(
        "OPEN_RANGE_ALT",
        "Opening Range Breakout Legacy",
        "Legacy ORB with fixed minute window used only for regression coverage.",
    ),
    "dark_pool_radar_old": BotMeta(
        "DARK_POOL_LEGACY",
        "Dark Pool Radar Legacy",
        "Legacy dark pool logic kept for regression parity; uses older sizing heuristics.",
    ),
}


def get_strategy_tag(bot_name: str) -> str:
    meta = BOT_METADATA.get(bot_name.lower())
    return meta.strategy_tag if meta else "UNKNOWN"


def get_bot_meta(bot_name: str) -> BotMeta | None:
    return BOT_METADATA.get(bot_name.lower())


# Ensure templates carry tags for alert uniqueness enforcement
for _name, _meta in list(BOT_METADATA.items()):
    if _meta.strategy_tag not in _meta.title_template:
        BOT_METADATA[_name] = BotMeta(
            _meta.strategy_tag,
            f"[{_meta.strategy_tag}] {_meta.title_template}",
            _meta.why_template,
        )
