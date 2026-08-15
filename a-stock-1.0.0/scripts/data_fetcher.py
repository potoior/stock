import json
import urllib.request
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

CACHE_DIR = Path.home() / ".a-stock"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "stock_cache.db"

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily (
            code TEXT, date TEXT,
            open REAL, close REAL, high REAL, low REAL,
            volume REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.commit()
    return conn

def sina_symbol(code):
    code = code.upper().replace("SH", "").replace("SZ", "").replace(".", "")
    if code.startswith("6"):
        return "sh" + code
    elif code.startswith(("0", "3")):
        return "sz" + code
    elif code.startswith(("8", "4")):
        return "bj" + code
    return "sh" + code

def fetch_from_sina(code, datalen=240):
    symbol = sina_symbol(code)
    url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={symbol}&datalen={datalen}&scale=240&ma=no")
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        raw = json.loads(resp.read().decode("gbk"))
    except Exception:
        return pd.DataFrame()
    rows = []
    for item in raw:
        d = item["day"].replace("-", "")
        rows.append({
            "code": code, "date": d,
            "open": float(item["open"]), "close": float(item["close"]),
            "high": float(item["high"]), "low": float(item["low"]),
            "volume": float(item.get("volume", 0)),
        })
    return pd.DataFrame(rows)

def get_daily_data(code, days=180):
    end_date = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
    conn = init_db()
    cached = pd.read_sql(
        "SELECT * FROM daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        conn, params=(code, start, end_date)
    )
    if len(cached) > 0:
        cached_end = cached["date"].max()
        if cached_end >= end_date:
            conn.close()
            cached["date"] = pd.to_datetime(cached["date"])
            return cached
    df = fetch_from_sina(code)
    if len(df) > 0:
        conn.execute("DELETE FROM daily WHERE code=?", (code,))
        df.to_sql("daily", conn, if_exists="append", index=False)
    result = pd.read_sql(
        "SELECT * FROM daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        conn, params=(code, start, end_date)
    )
    result["date"] = pd.to_datetime(result["date"])
    conn.close()
    return result

def fetch_realtime(codes):
    symbols = [sina_symbol(c) for c in codes]
    url = "http://hq.sinajs.cn/list=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn/"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception:
        return []
    results = []
    lines = data.strip().split("\n")
    for i, code in enumerate(codes):
        if i >= len(lines):
            continue
        line = lines[i]
        if f"hq_str_{symbols[i]}" not in line:
            continue
        parts = line.split('"')[1].split(",") if '"' in line else []
        if len(parts) < 30:
            continue
        change = float(parts[3]) - float(parts[2])
        pct = change / float(parts[2]) * 100 if float(parts[2]) else 0
        results.append({
            "code": code, "name": parts[0],
            "price": float(parts[3]), "change": round(change, 2),
            "pct": round(pct, 2), "high": float(parts[4]),
            "low": float(parts[5]), "open": float(parts[1]),
            "yclose": float(parts[2]),
            "volume": int(parts[8]) if parts[8] else 0,
        })
    return results