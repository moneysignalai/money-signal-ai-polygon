<p align="center">
  <img src="docs/moneysignal-logo.png" alt="MoneySignalAI Logo" width="420">
</p>

<h1 align="center">💚 MoneySignalAI 💚</h1>

<p align="center">
  <b>15-in-1 Market Intelligence Bot Suite for Stocks, Options, Flow & Momentum</b><br>
  Built on <a href="https://polygon.io">Polygon.io</a> • Deployed on <a href="https://render.com">Render</a> • Alerts on Telegram
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/Framework-FastAPI-009688?logo=fastapi&logoColor=white" />
  <img src="https://img.shields.io/badge/Data-Polygon.io-00B3FF?logo=data:image/svg+xml;base64,IA==" />
  <img src="https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render&logoColor=white" />
  <img src="https://img.shields.io/badge/Alerts-Telegram-26A5E4?logo=telegram&logoColor=white" />
</p>

---

## ⚡ What Is MoneySignalAI?

**MoneySignalAI** is a high-octane, async scanner that runs **multiple alpha bots at once**, watches the whole US equities market, and pushes **clean, emoji-styled alerts** to your Telegram.

Instead of staring at charts all day, you get:

- 🐋 **Whale options flow**
- 🧊 **IV crush after earnings**
- 🌑 **Dark pool clusters**
- 🔥 **Cheap 0DTE plays**
- 📈 **Daily breakouts**
- 💥 **Panic flush wipeouts**
- 🔄 **A+ pullbacks in strong trends**
- …all in **one bot suite**, running automatically.

---

## 📊 Included Bots (15 Total)

### 🔥 High-Conviction Options Bots

| # | Bot | What It Hunts | Time (EST) | Type |
|---|-----|---------------|-----------|------|
| 1 | **Cheap 0DTE / 3DTE Hunter** | Cheap weekly options on $10–$80 names with high IV + RVOL surge | 9:30–16:00 | Options |
| 2 | **Unusual Options Sweeps** | Big call/put sweeps and concentrated premium in one contract | 9:30–16:00 | Options |
| 3 | **Whales** | Single-contract orders with notional ≥ \$2M (CALLS + PUTS) | 9:30–16:00 | Options |
| 4 | **IV Crush / Earnings Post-Mortem** | Day-over-day IV collapse vs actual move after earnings/events | 7:00–16:00 | Options |

---

### 📈 Momentum, Breakouts & Reversals

| # | Bot | What It Hunts | Time (EST) | Type |
|---|-----|---------------|-----------|------|
| 5 | **ORB (Opening Range Breakout)** | 15-min ORB + clean 5-min confirmation, with RVOL filters | 9:45–11:00 | Price Action |
| 6 | **Gap & Go / Gap Down** | Overnight gap up/down + strong open volume, low junk | 9:30–10:30 | Price Action |
| 7 | **Momentum Reversal** | Overextended intraday runs that start reversing with volume | 11:30–16:00 | Price Action |
| 8 | **Trend Rider** | 20 EMA > 50 EMA and breakout > 20-day high (or breakdown < 20-day low) | 15:30–20:00 | Daily Trend |
| 9 | **Swing Pullback** | Strong uptrend + multi-day dip + bounce near 20 EMA | 9:30–16:00 | Swing |
|10 | **Panic Flush** | -12%+ down days near 52-week lows with huge RVOL | 9:30–16:00 | Capitulation |
|11 | **Volume Monster** | 1-minute bars with insane relative volume | 9:30–16:00 | Analytics |

---

### 🌑 Events, Liquidity & System Health

| # | Bot | What It Hunts | Time (EST) | Type |
|---|-----|---------------|-----------|------|
|12 | **Pre-Market Runner** | +8% premarket movers with real volume | 4:00–9:29 | Pre-Market |
|13 | **Earnings Catalyst** | Stocks with upcoming earnings + RVOL “loading” | 7:00–22:00 | Events |
|14 | **Dark Pool Radar** | Clusters of dark/ATS prints (10M–50M+) over last X minutes | 4:00–20:00 | Liquidity |
|15 | **Status / Health Bot** | Scan cycles, errors, environment sanity pings | Scheduled | Utility |

---

## 🧱 Architecture (High Level)

```text
main.py
 ├─ FastAPI app (health endpoint /)
 ├─ background loop (every 60s)
 └─ launches all bots concurrently (asyncio.gather)

bots/
 ├─ cheap.py             # Cheap 0DTE / 3DTE
 ├─ unusual.py           # Unusual sweeps / flow
 ├─ whales.py            # $2M+ whale orders
 ├─ iv_crush.py          # Earnings IV crush
 ├─ dark_pool_radar.py   # Dark/ATS clusters
 ├─ panic_flush.py       # True capitulation
 ├─ swing_pullback.py    # A+ uptrend pullbacks
 ├─ trend_rider.py       # Daily breakouts
 ├─ volume.py            # Volume monster
 ├─ orb.py               # Opening Range Breakout
 ├─ gap.py               # Gap up / gap down
 ├─ premarket.py         # Pre-market runners
 ├─ earnings.py          # Earnings calendar / movers
 ├─ momentum_reversal.py # Late-day reversals
 └─ status_report.py     # System heartbeat

bots/shared.py
 ├─ POLYGON_KEY, global RVOL/volume thresholds
 ├─ send_alert() / send_status()
 ├─ dynamic most-active universe builder
 ├─ equity setup grading (A+, A, B, C)
 └─ small helpers: chart_link(), is_etf_blacklisted(), etc.

