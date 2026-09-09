"""数据获取与缓存层: 日K线(腾讯前复权/新浪) + 全市场行情 + 实时价。

stock_cache.db 的 daily 表是全系统唯一的日 K 线缓存:
- get_daily_data(): 读缓存,不新鲜则拉腾讯前复权并回写
- _bulk_fetch_daily(): 全市场扫描用批量读(只读不联网)
- fetch_market_all/page(): 新浪全市场实时行情
- fetch_realtime(): 新浪实时价
"""

import atexit
import json
import logging
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

DB_PATH = Path(__file__).parent / "stock_cache.db"
log = logging.getLogger("quant.fetcher")


CACHE_DB = Path(__file__).parent / "stock_cache.db"

# 模块级一次性建表 + WAL,避免每次 connect 都 CREATE TABLE,且 16 线程并发不阻塞
_daily_table_inited = False
_daily_table_lock = threading.Lock()

# 线程局部 sqlite 连接复用（避免 1700+ 次 connect/close 开销）
_tl = threading.local()


def _get_db_conn() -> sqlite3.Connection:
    conn = getattr(_tl, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(CACHE_DB), timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        _tl.conn = conn
    return conn


def _ensure_daily_table():
    global _daily_table_inited
    if _daily_table_inited:
        return
    with _daily_table_lock:
        if _daily_table_inited:
            return
        conn = sqlite3.connect(str(CACHE_DB), timeout=10)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily (
                code TEXT, date TEXT,
                open REAL, close REAL, high REAL, low REAL,
                volume REAL, PRIMARY KEY (code, date)
            )""")
        # 复合索引:加速 WHERE code=? (450万行全表扫 12ms→<1ms)
        # 和 GROUP BY code (29s→0.3s, 玉姐扫描瓶颈)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_code_date ON daily(code, date)"
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        conn.commit()
        conn.close()
        _daily_table_inited = True


# ---------------- 数据获取（腾讯前复权） ----------------

# httpx 连接池复用 TCP（避免 urllib 每次都 DNS+TCP+TLS 握手）
_http_client = httpx.Client(
    timeout=15.0,
    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
    trust_env=False,  # 绕过 socks 代理
    headers={"Referer": "https://gu.qq.com/", "User-Agent": "Mozilla/5.0"},
)


def _close_http_client():
    try:
        _http_client.close()
    except Exception:
        pass


atexit.register(_close_http_client)


def _sina_symbol(code):
    code = code.upper().replace("SH", "").replace("SZ", "").replace(".", "")
    if code.startswith("6"):
        return "sh" + code
    elif code.startswith(("0", "3")):
        return "sz" + code
    elif code.startswith(("8", "4")):
        return "bj" + code
    return "sh" + code


def fetch_qfq_tencent(code, datalen=320):
    symbol = _sina_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{datalen},qfq"
    try:
        resp = _http_client.get(url)
        raw = resp.json()
    except Exception:
        return _fetch_kline_sina(code, datalen)
    node = raw.get("data", {}).get(symbol, {})
    rows_raw = node.get("qfqday") or node.get("day") or []
    rows = []
    for item in rows_raw:
        vol = (float(item[5]) * 100) if len(item) > 5 and item[5] else 0
        rows.append(
            {
                "code": code,
                "date": item[0].replace("-", ""),
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": vol,
            }
        )
    if not rows:
        # 腾讯返回空, fallback 到新浪
        return _fetch_kline_sina(code, datalen)
    return pd.DataFrame(rows)


def _fetch_kline_sina(code, datalen=320):
    """新浪日 K 线接口(腾讯 WAF 拦截时的 fallback)。
    URL: quotes.sina.cn/cn/api/jsonp_v2.php/.../CN_MarketDataService.getKLineData
    返回 JSONP: var=([{day,open,high,low,close,volume,...}, ...])
    """
    symbol = _sina_symbol(code)
    url = (
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var=/CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=240&datalen={datalen}"
    )
    try:
        resp = _http_client.get(url)
        text = resp.text
        # 提取 var=(...) 中的 JSON
        start = text.find("=(")
        if start < 0:
            return pd.DataFrame()
        json_str = text[start + 2 : text.rfind(")")]
        items = json.loads(json_str)
    except Exception:
        return pd.DataFrame()
    rows = []
    for item in items:
        rows.append(
            {
                "code": code,
                "date": item["day"].replace("-", ""),
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item["volume"]),
            }
        )
    return pd.DataFrame(rows)


def get_daily_data(code: str, days: int = 320) -> pd.DataFrame:
    _ensure_daily_table()
    conn = _get_db_conn()
    df = pd.read_sql("SELECT * FROM daily WHERE code=? ORDER BY date", conn, params=(code,))
    fresh = False
    if len(df) > 0:
        last = df["date"].max()
        today = datetime.now().strftime("%Y%m%d")
        if last >= today:
            fresh = True
    if not fresh:
        newdf = fetch_qfq_tencent(code, datalen=days)
        if len(newdf) > 0:
            conn.executemany(
                "INSERT OR REPLACE INTO daily(code,date,open,close,high,low,volume) VALUES(?,?,?,?,?,?,?)",
                newdf[["code", "date", "open", "close", "high", "low", "volume"]].values.tolist(),
            )
            conn.commit()
            df = newdf  # 直接用刚拉到的数据,跳过第二次 read_sql
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.sort_values("date").reset_index(drop=True)
    return df.tail(days).reset_index(drop=True)

# ---------------- 全市场实时行情(新浪) ----------------

HQ_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def fetch_market_page(page=1, num=100, sort="amount", asc=0):
    url = f"{HQ_URL}?page={page}&num={num}&sort={sort}&asc={asc}&node=hs_a&symbol=&_s_r_a=page"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.sina.com.cn/", "User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk")
    return json.loads(raw) or []


def norm_code(symbol):
    return symbol[2:] if symbol[:2].lower() in ("sh", "sz", "bj") else symbol


def fetch_market_all(limit=0, max_pages=80):
    """抓取全市场 A 股实时行情,返回标准化字典列表。

    每页失败重试 3 次(指数退避 1s/2s/4s),应对开盘瞬间 HTTP 456 限流。
    仍失败才跳过该页继续下一页,避免单页网络抖动丢掉后面所有页。
    """
    rows = []
    page = 1
    while page <= max_pages:
        batch = None
        last_err = None
        for attempt in range(3):  # 指数退避 1, 2 秒
            try:
                batch = fetch_market_page(page=page, num=100, sort="amount", asc=0)
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1, 2 秒
        if batch is None:
            log.warning("fetch_market_page page=%d 重试 3 次仍失败: %s,跳过", page, last_err)
            page += 1
            continue
        if not batch:
            break  # 真正的尾页(空数据)才退出
        rows.extend(batch)
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        if len(batch) < 100:
            break
        page += 1
    for r in rows:
        r["code6"] = norm_code(r.get("symbol", ""))[-6:].zfill(6)
    return rows

def fetch_realtime(codes):
    if not codes:
        return []
    symbols = [_sina_symbol(c) for c in codes]
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
        results.append(
            {
                "code": code,
                "name": parts[0],
                "price": float(parts[3]),
                "change": round(change, 2),
                "pct": round(pct, 2),
                "high": float(parts[4]),
                "low": float(parts[5]),
                "open": float(parts[1]),
                "yclose": float(parts[2]),
                "volume": int(parts[8]) if parts[8] else 0,
            }
        )
    return results
