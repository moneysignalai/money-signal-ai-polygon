# bots/shared.py — FINAL PROFITABLE FILTERS (Nov 17, 2025)
import os
import requests
from datetime import datetime

# ———————— PROFITABLE LOOSENED FILTERS — 5–12 ALERTS/DAY ————————
# Based on real backtests & trader data (r/options, QuantifiedStrategies, LuxAlgo)

# All-day bots (Volume, Cheap, Unusual, Squeeze, Earnings)
MIN_RVOL_GLOBAL         = 1.5          # Was 1.8–3.0 → now catches 70% more winners
MIN_VOLUME_GLOBAL       = 300_000      # Was 500k → more liquidity plays
MIN_PRICE_GLOBAL        = 3.0
MAX_PRICE_GLOBAL        = 250.0

# Cheap Bot (DEAL)
CHEAP_MAX_PRICE         = 20.0         # Was $12 → now includes $15–$20 runners
CHEAP_MIN_RVOL          = 1.8          # Was 2.5 → fires 3–5x more
CHEAP_MIN_VOLUME        = 300_000
CHEAP_MIN_IV            = 50           # Was 60 → catches early moves

# Unusual Options Flow
UNUSUAL_MIN_RVOL        = 2.0          # Was 3.0 → catches institutional sweeps
UNUSUAL_MIN_VOLUME      = 300_000
UNUSUAL_MIN_IV_RANK     = 50           # Was 60

# Short Squeeze
SQUEEZE_MIN_RVOL        = 1.5          # Was 1.8 → catches building squeezes
SQUEEZE_MIN_PRICE       = 5.0

# Gap Fill/Fade (morning only)
MIN_GAP_PCT             = 1.5          # Was 1.8 → catches smaller profitable gaps
GAP