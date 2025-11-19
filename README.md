my<p align="center">
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

⚙️ Local Installation

1️⃣ Clone the repo

git clone https://github.com/YOURNAME/money-signal-ai.git
cd money-signal-ai

2️⃣ Create a virtualenv (optional but recommended)

python -m venv .venv
source .venv/bin/activate      # macOS / Linux
# or
.\.venv\Scripts\activate       # Windows

3️⃣ Install requirements

pip install -r requirements.txt

4️⃣ Environment variables

Create a .env file or set these directly in your environment / Render dashboard.

🔑 Core

POLYGON_KEY=your_polygon_api_key

TELEGRAM_TOKEN_ALERTS=your_telegram_bot_token
TELEGRAM_CHAT_ALL=your_main_alert_chat_id

TELEGRAM_TOKEN_STATUS=optional_status_bot_token
TELEGRAM_CHAT_STATUS=optional_status_chat_id

🌍 Global filters (for all bots)

MIN_RVOL_GLOBAL=2.5         # RVOL floor
MIN_VOLUME_GLOBAL=800000    # minimum daily volume in shares

🎯 Optional per-bot tuning (override defaults)

These are optional; the code includes safe defaults. Only set them if you want to be more aggressive / conservative.

# WHALES
WHALES_MIN_NOTIONAL=2000000
WHALES_MIN_PRICE=10
WHALES_MAX_PRICE=500

# DARK POOL RADAR
DARK_LOOKBACK_MIN=20
DARK_MIN_TOTAL_NOTIONAL=20000000
DARK_MIN_SINGLE_NOTIONAL=10000000
DARK_MIN_PRINT_COUNT=3

# TREND RIDER
TREND_BREAKOUT_LOOKBACK=20
TREND_BREAKOUT_MIN_PCT=2.0
TREND_MIN_RVOL=3.0

# PANIC FLUSH
PANIC_MIN_DROP_PCT=12
PANIC_MIN_RVOL=4.0
PANIC_NEAR_LOW_PCT=2.0

# SWING PULLBACK
PULLBACK_MAX_DIST_EMA=1.0
PULLBACK_MIN_RVOL=2.0

# IV CRUSH
IVCRUSH_MAX_DTE=14
IVCRUSH_MIN_IV=0.6
IVCRUSH_MIN_IV_DROP_PCT=30
IVCRUSH_MIN_IMPLIED_MOVE_PCT=8
IVCRUSH_MAX_MOVE_REL_IV=0.6

🚀 Running Locally

Run the FastAPI + background scanner

uvicorn main:app --reload --host 0.0.0.0 --port 8000

This will:
	•	Start a small API (for Render health checks).
	•	Spin up a background task that:
	•	Every ~60 seconds:
	•	Builds the dynamic universe.
	•	Runs all 15 bots concurrently.
	•	Sends alerts to Telegram.

Visit:

http://localhost:8000/

to confirm the service is up.

🌥 Deploying on Render
	1.	Push your repo to GitHub.
	2.	Go to Render → New → Web Service.
	3.	Connect to your GitHub repo.
	4.	Choose:
	•	Runtime: Python
	•	Start Command:

gunicorn -k uvicorn.workers.UvicornWorker main:app --timeout 600

5.	In Environment → Environment Variables, add all POLYGON_KEY, TELEGRAM_*, MIN_*, etc.
	6.	Deploy.

Render will:
	•	Health-check /
	•	Keep the process alive
	•	Let the async scanner run 24/5.


🧪 Sample Alert Formats

These are examples of how Telegram messages look.
In your own alerts the tickers, numbers, and times will be live.

🐋 Whale Flow
🐋 WHALE FLOW — META
🕒 10:35 AM EST · Nov 21
💰 Underlying: $317.22 · RVOL 3.9x
────────────
🟢 META 11/24 330C
📦 Volume: 4,812 · Avg: $6.15
💰 Notional: ≈ $2,961,000
🔗 Chart: https://www.tradingview.com/chart/?symbol=META

🌑 Dark Pool Radar
🌑 DARK POOL RADAR — AMD
🕒 7:42 PM EST · Nov 21
💰 $117.88 · RVOL 2.8x
────────────
📡 Dark pool cluster (last 20 min)
📦 Prints: 12
💰 Total Dark Notional: ≈ $47,550,000
🏦 Largest Single Print: ≈ $12,800,000
🔗 Chart: https://www.tradingview.com/chart/?symbol=AMD

🔥 Cheap 0DTE / 3DTE
🔥 CHEAP — BBAI
🕒 12:14 PM EST · Nov 21
💰 Last: $5.98
────────────
🔥 Cheap CALL: O:BBAI251121P00006000
⏱ DTE: 3 · Strike: 6.00
📦 Volume: 2,666 · Avg: $0.35
💰 Notional: ≈ $94,525
🔗 Chart: https://www.tradingview.com/chart/?symbol=BBAI


🛠 Developer Notes
	•	All bots are structured as async coroutines (async def run_xxx()).
	•	bots/shared.py centralizes:
	•	Global ENV
	•	Telegram sending
	•	Universe building
	•	Equity grading
	•	New bots are easy to add:
	1.	Create bots/new_strategy.py with async def run_new_strategy().
	2.	Import and add it to the asyncio.gather() list in main.py.
	3.	Use send_alert("new_strategy", ticker, price, rvol, extra=msg).


❓ FAQ

❓ Why am I not getting any alerts?

Check:
	1.	Are your Telegram tokens and chat IDs correct?
	2.	Do logs show:
SCANNING: Premarket, Volume, Gaps, ORB, ...

3.	Are your global filters too strict?
	•	Try temporarily:
MIN_RVOL_GLOBAL=2.0
MIN_VOLUME_GLOBAL=500000

❓ Why am I getting too many alerts?
	•	Raise thresholds:
MIN_RVOL_GLOBAL=3.0
MIN_VOLUME_GLOBAL=1500000
WHALES_MIN_NOTIONAL=3000000
DARK_MIN_TOTAL_NOTIONAL=40000000

	•	Or narrow the universe to only specific tickers via:
TICKER_UNIVERSE=AAPL,MSFT,TSLA,NVDA,META,AMZN

❓ Does this place trades for me?
No.
This is an information and alert system only. You (or your own trading system) decide whether to trade.

🧭 Roadmap
	•	🧬 Greeks Extreme Bot (gamma, vanna, charm pressure extremes)
	•	⚖️ Options vs Equity Divergence Bot (flow doesn’t match price)
	•	🧲 Liquidity Vacuum Detector (thin-book sweeps)
	•	📅 Pre-Earnings IV Ramp scanner
	•	🧂 Mean Reversion Bot for intraday overextensions
	•	🌐 Multi-exchange support (if Polygon adds more feeds)


⚠️ Disclaimer

This project is for educational and informational purposes only.
Nothing in this repository is financial advice.
Markets are risky. Do your own research. Use at your own risk.


🤝 Contributing

PRs, issues, and ideas are welcome.
	1.	Fork the repo
	2.	Create a feature branch
	3.	Add logs + comments
	4.	Submit a PR describing:
	•	What strategy you added/changed
	•	Example alert
	•	Any new ENV vars

<p align="center">
  Built for traders who don’t have time to babysit every chart. ⚡<br>
  <b>Let the bots watch the market. You just watch the alerts.</b>
</p>
```

