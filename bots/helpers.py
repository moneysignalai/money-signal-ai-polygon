from polygon import RESTClient
import os

client = RESTClient(os.getenv("POLYGON_KEY"))

def get_top_volume_stocks(limit=150):
    try:
        snapshot = client.get_snapshot_all(tickers=None)
        stocks = []
        for s in snapshot:
            if hasattr(s, 'day') and s.day and getattr(s.day, 'v', 0) > 200_000:
                stocks.append((s.ticker, getattr(s.day, 'v', 0)))
        stocks.sort(key=lambda x: x[1], reverse=True)
        return [x[0] for x in stocks[:limit]]
    except:
        return ["NVDA","TSLA","AAPL","AMD","SMCI","SPY","QQQ","IWM","T","F","AMC","GME","PLTR","SOFI","HOOD","MARA","RIOT","CLSK","CLOV","RIVN","LCID"]