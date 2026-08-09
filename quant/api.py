import json
import sqlite3
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from data_fetcher import get_daily_data, fetch_realtime

app = FastAPI(title="A股量化监控系统")
DB_PATH = Path(__file__).parent / "stock_cache.db"

# 默认股票池
DEFAULT_CODES = ["600789", "000001", "600519", "601318", "000333", "002415"]

def compute_signals(code):
    try:
        df = get_daily_data(code, "20240101")
        if len(df) < 60:
            return {}
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        price = close.iloc[-1]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_val = 2 * (dif.iloc[-1] - dea.iloc[-1])
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        return {
            "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
            "macd": round(macd_val, 3),
            "macd_bull": bool(dif.iloc[-1] > dea.iloc[-1]),
            "k": round(k.iloc[-1], 1), "d": round(d.iloc[-1], 1),
            "kdj_signal": "超卖" if k.iloc[-1] < 20 else ("超买" if k.iloc[-1] > 80 else "中性"),
        }
    except Exception:
        return {}

@app.get("/api/quotes")
def get_quotes(codes: str = ""):
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else DEFAULT_CODES
    quotes = fetch_realtime(code_list)
    result = []
    for q in quotes:
        sig = compute_signals(q["code"])
        result.append({
            "code": q["code"], "name": q["name"],
            "price": q["price"], "change": q["change"], "pct": q["pct"],
            "high": q["high"], "low": q["low"], "open": q["open"],
            "volume": q["volume"],
            "ma5": sig.get("ma5"), "ma10": sig.get("ma10"), "ma20": sig.get("ma20"),
            "macd": sig.get("macd"), "macd_bull": sig.get("macd_bull"),
            "k": sig.get("k"), "d": sig.get("d"), "kdj_signal": sig.get("kdj_signal"),
        })
    return {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": result}

@app.get("/api/kline/{code}")
def get_kline(code: str, days: int = 120):
    df = get_daily_data(code, "20240101")
    df = df.tail(days)
    points = []
    for _, row in df.iterrows():
        points.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "open": float(row["open"]), "close": float(row["close"]),
            "high": float(row["high"]), "low": float(row["low"]),
            "volume": float(row["volume"]),
        })
    return {"code": code, "data": points}

@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "web" / "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)