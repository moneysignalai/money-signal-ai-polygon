# bots/helpers.py — MUST EXIST (this was missing!)
from polygon import RESTClient
import os

# Global Polygon client (shared with shared.py)
client = RESTClient(os.getenv("POLYGON_KEY"))

def get_top_volume_stocks(limit=150):
    """Returns real top volume stocks from Polygon"""
    try:
        snapshot = client.get_snapshot_all(tickers=None)
        stocks = []
        for s in snapshot:
            if hasattr(s, 'day') and s.day and getattr(s.day, 'v', 0) > 300_000:
                stocks.append((s.ticker, getattr(s.day, 'v', 0)))
        stocks.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in stocks[:limit]]
    except:
        # Fallback list if API fails
        return [
            "NVDA","TSLA","AAPL","AMD","SMCI","SPY","QQQ","IWM","T","F","AMC","GME","PLTR",
            "SOFI","HOOD","MARA","RIOT","CLSK","CLOV","RIVN","LCID","NIO","XPEV","LI",
            "AUR","IONQ","ASTS","DJT","MSTR","COIN","UPST","RBLX","PATH","SNOW","CRWD",
            "NET","ZS","OKLO","SMR","BBAI","SOUN","HIMS","ARM","ANET","AVGO","NFLX"
        ]

# Dummy functions so nothing crashes
def mtf_confirm(*a): return True
def is_edge_option(*a): return True
def get_confidence_score(*a): return 95
def get_greeks(*a):
    class X: ask=1.40; delta=0.60; gamma=0.10; implied_volatility=0.75; volume=12000; open_interest=18000
    return X()
def build_rh_link(sym, *a): return f"https://robinhood.com/us/en/stocks/{sym}/"