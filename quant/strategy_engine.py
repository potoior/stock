import atexit
import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from data_fetcher import fetch_realtime  # re-export: 保持 se.fetch_realtime 向后兼容

log = logging.getLogger("quant")

ENGINE_HOME = Path(__file__).parent
CACHE_DB = ENGINE_HOME / "stock_cache.db"
CONFIG_PATH = ENGINE_HOME / "config.json"

# 模块级一次性建表 + WAL,避免每次 connect 都 CREATE TABLE,且 16 线程并发不阻塞
_daily_table_inited = False
_daily_table_lock = threading.Lock()

# config.json 进程级缓存（mtime 比较）
_config_cache = {"mtime": 0, "data": None}
_config_lock = threading.Lock()

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


# ---------------- 指标计算（通达信口径） ----------------


def compute_basic_signals(code: str, current_price: float | None = None) -> dict:
    """统一的简化信号计算：返回 MA/MACD/KDJ 基础指标字典。

    被 api.py / dashboard.py / realtime.py / agent.py 共用，避免四份重复实现。
    若 current_price 给定，则用其替换最后一根 K 线的收盘价/最高/最低，模拟实时价。
    MACD/KDJ 复用 compute_macd/compute_kdj，确保口径与策略端完全一致。
    """
    try:
        df = get_daily_data(code)
        if len(df) < 60:
            return {}
        df = df.sort_values("date").reset_index(drop=True)
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        if current_price is not None:
            close = pd.concat([close[:-1], pd.Series([current_price])], ignore_index=True)
            high = pd.concat([high[:-1], pd.Series([max(high.iloc[-1], current_price)])], ignore_index=True)
            low = pd.concat([low[:-1], pd.Series([min(low.iloc[-1], current_price)])], ignore_index=True)

        last_price = float(current_price if current_price is not None else close.iloc[-1])
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        # 复用统一指标实现，避免重复且口径漂移
        tmp = pd.DataFrame({"close": close, "high": high, "low": low})
        diff, dea, bar = compute_macd(tmp)
        k, d, j, _ = compute_kdj(tmp)
        k_val = float(k.iloc[-1])
        d_val = float(d.iloc[-1])
        j_val = float(j.iloc[-1])
        return {
            "price": last_price,
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "macd": round(float(bar.iloc[-1]), 3),
            "macd_bull": bool(diff.iloc[-1] > dea.iloc[-1]),
            "k": round(k_val, 1),
            "d": round(d_val, 1),
            "j": round(j_val, 1),
            "kdj_signal": "超卖" if k_val < 20 else ("超买" if k_val > 80 else "中性"),
            "above_ma5": bool(last_price > ma5),
        }
    except Exception:
        return {}


def compute_macd(df, fast=12, slow=26, signal=9):
    close = df["close"]
    diff = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = diff.ewm(span=signal, adjust=False).mean()
    bar = (diff - dea) * 2
    return diff, dea, bar


def compute_kdj(df, n=9, k1=3, d1=3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    spread = (high_n - low_n).replace(0, np.nan)
    rsv = (df["close"] - low_n) / spread * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=k1 - 1, adjust=False).mean()
    d = k.ewm(com=d1 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j, rsv


def compute_boll(df, period=20, std=2):
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std(ddof=0)
    return mid + sd * std, mid, mid - sd * std


def compute_psy(df, period=12):
    up = (df["close"] > df["close"].shift(1)).astype(float)
    return up.rolling(period).sum() / period * 100


def compute_bias(df, p1=6, p2=12, p3=24):
    c = df["close"]
    b1 = (c - c.rolling(p1).mean()) / c.rolling(p1).mean() * 100
    b2 = (c - c.rolling(p2).mean()) / c.rolling(p2).mean() * 100
    b3 = (c - c.rolling(p3).mean()) / c.rolling(p3).mean() * 100
    return b1, b2, b3


def compute_bbiboll(df, m1=3, m2=6, m3=12, m4=24, n=11, m=6):
    ma1 = df["close"].rolling(m1).mean()
    ma2 = df["close"].rolling(m2).mean()
    ma3 = df["close"].rolling(m3).mean()
    ma4 = df["close"].rolling(m4).mean()
    bbi = (ma1 + ma2 + ma3 + ma4) / 4
    sd = bbi.rolling(n).std(ddof=0)
    return bbi + sd * m, bbi, bbi - sd * m


def compute_tower(df):
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(close)
    tower = np.zeros(n)
    for i in range(1, n):
        if close[i] > high[i - 1]:
            tower[i] = 1  # 突破前一根最高价 → 翻红
        elif close[i] < low[i - 1]:
            tower[i] = -1  # 跌破前一根最低价 → 翻绿
        else:
            tower[i] = tower[i - 1]  # 未突破/未跌破 → 延续前态
    return pd.Series(tower, index=df.index)


def compute_rsi(df, p1=6, p2=12):
    """RSI 相对强弱指标，返回 (RSI短线, RSI长线)。"""
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    def _rsi(period):
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        return rsi.fillna(50)

    return _rsi(p1), _rsi(p2)


def compute_mos_lows(df, diff=None, dea=None):
    """MOS 看盘系统（通达信公式精确实现）。

    原公式：
      DIFF:=100*(EMA(CLOSE,12)-EMA(CLOSE,26))
      DEA:=EMA(DIFF,9)
      死叉:=CROSS(DEA,DIFF)
      N1:=BARSLAST(死叉)  N2:=REF(BARSLAST(死叉),N1+1)
      CL1:=LLV(LOW,N1+1)  DIFL1:=LLV(DIFF,N1+1)
      CL2:=REF(CL1,N1+1)  DIFL2:=REF(DIFL1,N1+1)
    返回 dict: cl1/cl2/cl3, difl1/difl2/difl3, n1, 以及
      bottom  = CL1<CL2 且 DIFL1>=DIFL2（底背离低点）
    """
    if diff is None:
        diff, dea, _ = compute_macd(df)
    low = df["low"].values
    dif_arr = diff.values
    n = len(df)

    # 死叉点: DEA 上穿 DIFF
    death = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if dea.iloc[i] > diff.iloc[i] and dea.iloc[i - 1] <= diff.iloc[i - 1]:
            death[i] = True

    if death.sum() == 0:
        return {
            "cl1": None, "cl2": None, "cl3": None,
            "difl1": None, "difl2": None, "difl3": None,
            "n1": n, "bottom": False,
        }

    death_idx = np.where(death)[0]
    last = death_idx[-1]
    n1 = n - 1 - last
    _cl1 = low[last:].min()
    _dl1 = dif_arr[last:].min()
    if len(death_idx) >= 2:
        prev = death_idx[-2]
        cl2 = low[prev : last + 1].min()
        dl2 = dif_arr[prev : last + 1].min()
    else:
        cl2 = low[: last + 1].min()
        dl2 = dif_arr[: last + 1].min()
    if len(death_idx) >= 3:
        pp = death_idx[-3]
        cl3 = low[pp : prev + 1].min()
        dl3 = dif_arr[pp : prev + 1].min()
    else:
        cl3 = low.min()
        dl3 = dif_arr.min()

    return {
        "cl1": float(_cl1),
        "cl2": float(cl2),
        "cl3": float(cl3),
        "difl1": float(_dl1),
        "difl2": float(dl2),
        "difl3": float(dl3),
        "n1": int(n1),
        "bottom": bool(_cl1 < cl2 and _dl1 >= dl2),
    }


def compute_dmi(df, n=14, m=6):
    high, low, close = df["high"], df["low"], df["close"]
    ph, pl, pc = high.shift(1), low.shift(1), close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    hd, ld = high - ph, pl - low
    dmp = pd.Series(np.where((hd > 0) & (hd > ld), hd, 0), index=df.index)
    dmm = pd.Series(np.where((ld > 0) & (ld > hd), ld, 0), index=df.index)
    mtr = tr.rolling(n).sum()
    dmp_s = dmp.rolling(n).sum()
    dmm_s = dmm.rolling(n).sum()
    pdi = pd.Series(np.where(mtr > 0, 100 * dmp_s / mtr, 0), index=df.index)
    mdi = pd.Series(np.where(mtr > 0, 100 * dmm_s / mtr, 0), index=df.index)
    dx = pd.Series(np.where(pdi + mdi > 0, 100 * (pdi - mdi).abs() / (pdi + mdi), 0), index=df.index)
    adx = dx.rolling(m).mean()
    return pdi, mdi, adx


def compute_sar(df, af_init=0.02, af_max=0.2):
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(close)
    sar = np.zeros(n)
    trend = np.zeros(n)
    af = np.zeros(n)
    ep = np.zeros(n)
    sar[0] = close[0]
    trend[0] = 1 if close[0] >= close[min(4, n - 1)] else -1
    af[0] = af_init
    ep[0] = high[0] if trend[0] >= 0 else low[0]
    for i in range(1, n):
        if trend[i - 1] >= 0:
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])
            if i > 0:
                sar[i] = min(sar[i], low[i - 1])
            if i > 1:
                sar[i] = min(sar[i], low[i - 2])
            if sar[i] > low[i]:
                trend[i] = -1
                sar[i] = ep[i - 1]
                af[i] = af_init
                ep[i] = low[i]
            else:
                trend[i] = 1
                if high[i] > ep[i - 1]:
                    ep[i] = high[i]
                    af[i] = min(af[i - 1] + af_init, af_max)
                else:
                    ep[i] = ep[i - 1]
                    af[i] = af[i - 1]
        else:
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])
            if i > 0:
                sar[i] = max(sar[i], high[i - 1])
            if i > 1:
                sar[i] = max(sar[i], high[i - 2])
            if sar[i] < high[i]:
                trend[i] = 1
                sar[i] = ep[i - 1]
                af[i] = af_init
                ep[i] = high[i]
            else:
                trend[i] = -1
                if low[i] < ep[i - 1]:
                    ep[i] = low[i]
                    af[i] = min(af[i - 1] + af_init, af_max)
                else:
                    ep[i] = ep[i - 1]
                    af[i] = af[i - 1]
    return sar, trend


# ---------------- 配置持久化 ----------------

DEFAULT_STRATEGY_PARAMS = {
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "kdj": {"n": 9, "k1": 3, "d1": 3},
    "ma_stop": {"period": 5},
    "boll": {"period": 20, "std": 2},
    "dmi": {"n": 14, "m": 6},
    "psy": {"period": 12},
    "bias": {"short": 3, "long": 5},
    "sar": {"af_init": 0.02, "af_max": 0.2},
    "ma_combo": {"short": 5, "mid": 10, "long": 60},
    "two_line": {"short": 5, "long": 10},
    "life_line": {"period": 60},
    "three_third": {"p1": 7, "p2": 13, "p3": 20},
    "sparrow": {"lookback": 5, "target": 2.5},
    "bounce": {"rebound_pct": 0.5, "vol_increase": 20},
    "volume_div": {"lookback": 10, "shrink": 0.7, "expand": 1.3},
    "dmi_psy": {"pdi_threshold": 5, "psy_threshold": 25},
}


def _load_config():
    with _config_lock:
        if CONFIG_PATH.exists():
            try:
                mtime = CONFIG_PATH.stat().st_mtime
                if mtime == _config_cache["mtime"] and _config_cache["data"] is not None:
                    return _config_cache["data"]
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _config_cache["mtime"] = mtime
                _config_cache["data"] = data
                return data
            except Exception:
                pass
        example = ENGINE_HOME / "config.example.json"
        if example.exists():
            try:
                _save_config(json.loads(example.read_text(encoding="utf-8")))
                # 重新读一次填充缓存
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _config_cache["mtime"] = CONFIG_PATH.stat().st_mtime
                _config_cache["data"] = data
                return data
            except Exception:
                pass
    return {}


def _save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        _config_cache["mtime"] = CONFIG_PATH.stat().st_mtime
        _config_cache["data"] = cfg
    except Exception:
        pass


# ---------------- AI 判定缓存（当日） ----------------


def _ai_cache_db():
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_cache (
            code TEXT, date TEXT, content TEXT, PRIMARY KEY (code, date)
        )""")
    conn.commit()
    return conn


def _ai_cache_get(code, date):
    try:
        conn = _ai_cache_db()
        row = conn.execute("SELECT content FROM ai_cache WHERE code=? AND date=?", (code, date)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _ai_cache_set(code, date, content):
    try:
        conn = _ai_cache_db()
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache(code, date, content) VALUES(?,?,?)", (code, date, content)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def clear_ai_cache(code=None):
    """清除AI判定缓存（策略变更后调用）。code=None清全部"""
    try:
        conn = _ai_cache_db()
        if code:
            conn.execute("DELETE FROM ai_cache WHERE code=?", (code,))
        else:
            conn.execute("DELETE FROM ai_cache")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _rule_to_text(lt):
    """旧式条件数组 [{metric,op,threshold}] → 人类可读规则文本"""
    parts = []
    for c in lt:
        m, op, t = c.get("metric", ""), c.get("op", ">"), c.get("threshold", 0)
        label = CONDITION_METRIC_META.get(m, m)
        opmap = {">": "大于", ">=": "大于等于", "<": "小于", "<=": "小于等于", "==": "等于", "is_true": ""}
        if op == "is_true":
            parts.append(f"{label}成立")
        else:
            parts.append(f"{label} {opmap.get(op, op)} {t}")
    return " 且 ".join(parts)


def migrate_custom_strategies(strategies):
    """旧 {buy:[conds], sell:[conds]} → 新 {buy_rule, sell_rule}，持久化保存"""
    migrated = False
    for s in strategies:
        if s.get("type") == "custom" and not s.get("buy_rule"):
            buy_text = _rule_to_text(s.get("buy", [])) if s.get("buy") else ""
            sell_text = _rule_to_text(s.get("sell", [])) if s.get("sell") else ""
            s["buy_rule"] = buy_text or f"{s.get('name', '自定义策略')}的买入规则"
            s["sell_rule"] = sell_text or "价格明显走弱时卖出"
            migrated = True
    if migrated:
        cfg = _load_config()
        full = cfg.get("strategies", [])
        for s in full:
            if s.get("type") == "custom" and not s.get("buy_rule"):
                match = next((c for c in strategies if c.get("id") == s.get("id")), None)
                if match:
                    s["buy_rule"] = match.get("buy_rule", "")
                    s["sell_rule"] = match.get("sell_rule", "")
        cfg["strategies"] = full
        _save_config(cfg)
    return strategies


def get_watchlist():
    return _load_config().get("watchlist", [])


def add_watch(code, name=""):
    cfg = _load_config()
    wl = cfg.setdefault("watchlist", [])
    for it in wl:
        if it["code"] == code:
            if name:
                it["name"] = name
            _save_config(cfg)
            return False
    wl.append({"code": code, "name": name})
    _save_config(cfg)
    return True


def remove_watch(code):
    cfg = _load_config()
    wl = cfg.get("watchlist", [])
    new = [it for it in wl if it["code"] != code]
    changed = len(new) != len(wl)
    cfg["watchlist"] = new
    _save_config(cfg)
    return changed


def get_strategies():
    cfg = _load_config()
    return cfg.get("strategies", [])


def save_strategies(strategies):
    cfg = _load_config()
    cfg["strategies"] = strategies
    _save_config(cfg)


# ---------------- 每个策略一个可独立开关/调参的评估函数 ----------------


def _cross_series(series):
    prev = series.shift(1)
    golden = (series > 0) & (prev <= 0)
    death = (series < 0) & (prev >= 0)
    return golden, death


def _ma_series(df, period):
    return df["close"].rolling(period).mean()


def strategy_macd(ctx, params):
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    signal = int(params.get("signal", 9))
    diff, dea, bar = compute_macd(ctx["df"], fast=fast, slow=slow, signal=signal)
    return _strategy_macd_impl(ctx, diff, dea)


def _strategy_macd_impl(ctx, diff, dea):
    i = ctx["i"]
    d, e = diff.iloc[i], dea.iloc[i]
    g, dth = _cross_series(diff - dea)
    below_zero = d < 0 and e < 0
    above_zero = d > 0 and e > 0
    if g.iloc[i] and above_zero:
        return "buy", f"零上金叉，DIFF({d:.2f})上穿DEA({e:.2f})，锦上添花"
    if g.iloc[i] and below_zero:
        pc = 0
        for t in range(i - 20, i):
            if t >= 1 and diff.iloc[t] > dea.iloc[t] and diff.iloc[t - 1] < dea.iloc[t - 1]:
                pc += 1
        more = "，可靠性高" if pc >= 1 else "，可能反弹"
        return "buy", f"零下金叉，DIFF({d:.2f})上穿DEA({e:.2f}){more}"
    if g.iloc[i]:
        return "buy", f"金叉，DIFF({d:.2f})上穿DEA({e:.2f})"
    if dth.iloc[i] and below_zero:
        return "sell", f"零下死叉，DIFF({d:.2f})下穿DEA({e:.2f})，继续下跌"
    if dth.iloc[i] and above_zero:
        return "hold", "零上死叉，多头回调，观望"
    if dth.iloc[i]:
        return "sell", f"死叉，DIFF({d:.2f})下穿DEA({e:.2f})"
    if d > e:
        if below_zero:
            return "buy", f"零下金叉后运行，DIFF({d:.2f})>DEA({e:.2f})，多头排列"
        return "buy", f"DIFF({d:.2f})>DEA({e:.2f})，多头运行"
    if d < e:
        if below_zero:
            return "sell", f"零下空头，DIFF({d:.2f})<DEA({e:.2f})"
        return "sell", f"DIFF({d:.2f})<DEA({e:.2f})，空头运行"
    return "hold", f"DIFF({d:.2f})≈DEA({e:.2f})，方向不明"


def strategy_kdj(ctx, params):
    n = int(params.get("n", 9))
    k1 = int(params.get("k1", 3))
    d1 = int(params.get("d1", 3))
    k, d, j, _ = compute_kdj(ctx["df"], n=n, k1=k1, d1=d1)
    return _strategy_kdj_impl(ctx, k, d)


def _strategy_kdj_impl(ctx, k, d):
    i = ctx["i"]
    k_v, d_v = k.iloc[i], d.iloc[i]
    if k_v < 20 and d_v < 30:
        return "buy", f"超卖区，K={k_v:.1f} D={d_v:.1f}，超卖严重"
    if k_v > 80 and d_v > 70:
        return "sell", f"超买区，K={k_v:.1f} D={d_v:.1f}，超买严重"
    if k_v > d_v and k.iloc[i - 1] <= d.iloc[i - 1]:
        return "buy", f"KDJ金叉，K={k_v:.1f}上穿D={d_v:.1f}"
    if k_v < d_v and k.iloc[i - 1] >= d.iloc[i - 1]:
        return "sell", f"KDJ死叉，K={k_v:.1f}下穿D={d_v:.1f}"
    if k_v < 20:
        return "hold", f"K={k_v:.1f} 超卖区，等待金叉确认"
    if k_v > 80:
        return "hold", f"K={k_v:.1f} 超买区，注意回落"
    return "hold", f"K={k_v:.1f} D={d_v:.1f}，中位运行"


def strategy_ma_stop(ctx, params):
    period = int(params.get("period", 5))
    price = ctx["price"]
    ma = _ma_series(ctx["df"], period).iloc[ctx["i"]]
    if pd.isna(ma):
        return "hold", f"MA{period}数据不足"
    if price > ma:
        return "buy", f"价格({price:.2f})站上MA{period}({ma:.2f})"
    return "sell", f"价格({price:.2f})跌破MA{period}({ma:.2f})"


def strategy_boll(ctx, params):
    period = int(params.get("period", 20))
    std = float(params.get("std", 2))
    u, m, lo = compute_boll(ctx["df"], period=period, std=std)
    i = ctx["i"]
    u, m, lo = u.iloc[i], m.iloc[i], lo.iloc[i]
    price = ctx["price"]
    if pd.isna(lo):
        return "hold", "BOLL数据不足"
    if price <= lo:
        return "buy", f"价格({price:.2f})触及下轨({lo:.2f})"
    if price >= u:
        return "sell", f"价格({price:.2f})触及上轨({u:.2f})"
    if price > m:
        return "buy", f"价格({price:.2f})在中轨({m:.2f})上方"
    return "sell", f"价格({price:.2f})在中轨({m:.2f})下方"


def strategy_dmi(ctx, params):
    n = int(params.get("n", 14))
    m = int(params.get("m", 6))
    pdi, mdi, adx = compute_dmi(ctx["df"], n=n, m=m)
    i = ctx["i"]
    pdi, mdi, adx = pdi.iloc[i], mdi.iloc[i], adx.iloc[i]
    if pd.isna(pdi):
        return "hold", "DMI数据不足"
    if adx < 20:
        return "hold", f"ADX({adx:.0f})<20，无明确趋势，盘整观望"
    if adx > 40 and pdi > mdi:
        return "buy", f"ADX({adx:.0f})高+PDI({pdi:.0f})>MDI({mdi:.0f})，强多头"
    if adx > 40 and pdi < mdi:
        return "sell", f"ADX({adx:.0f})高+MDI({mdi:.0f})>PDI({pdi:.0f})，强空头"
    if pdi > mdi:
        return "buy", f"PDI({pdi:.1f})>MDI({mdi:.1f})，多方主导(ADX={adx:.0f})"
    return "sell", f"MDI({mdi:.1f})>PDI({pdi:.1f})，空方主导(ADX={adx:.0f})"


def strategy_psy(ctx, params):
    period = int(params.get("period", 12))
    psy = compute_psy(ctx["df"], period=period)
    v = psy.iloc[ctx["i"]]
    if pd.isna(v):
        return "hold", "PSY数据不足"
    if v <= 25:
        return "buy", f"PSY={v:.0f}，超卖区，有望反弹"
    if v >= 75:
        return "sell", f"PSY={v:.0f}，超买区，获利盘多"
    return "hold", f"PSY={v:.0f}，25-75正常区间"


def strategy_bias(ctx, params):
    short = float(params.get("short", 3))
    long = float(params.get("long", 5))
    b1, b2, b3 = compute_bias(ctx["df"])
    i = ctx["i"]
    b1, b2, b3 = b1.iloc[i], b2.iloc[i], b3.iloc[i]
    if pd.isna(b1):
        return "hold", "BIAS数据不足"
    if b1 <= -short or b2 <= -long:
        return "buy", f"BIAS6={b1:.1f}% BIAS12={b2:.1f}%，超跌反弹"
    if b1 >= short or b2 >= long:
        return "sell", f"BIAS6={b1:.1f}% BIAS12={b2:.1f}%，超涨回调"
    return "hold", f"BIAS6={b1:.1f}% BIAS12={b2:.1f}% BIAS24={b3:.1f}%，正常区间"


def strategy_sar(ctx, params):
    af_init = float(params.get("af_init", 0.02))
    af_max = float(params.get("af_max", 0.2))
    sar, _trend = compute_sar(ctx["df"], af_init=af_init, af_max=af_max)
    sar_v = sar[ctx["i"]]
    if sar_v <= 0:
        return "hold", "SAR数据不足"
    if ctx["price"] > sar_v:
        return "buy", f"价格({ctx['price']:.2f})>SAR({sar_v:.2f})，翻红"
    return "sell", f"价格({ctx['price']:.2f})<SAR({sar_v:.2f})，翻绿"


def strategy_burnal(ctx, params):
    u, m, lo = compute_bbiboll(ctx["df"])
    i = ctx["i"]
    bu, bm, bl = u.iloc[i], m.iloc[i], lo.iloc[i]
    price = ctx["price"]
    if pd.isna(bm):
        return "hold", "BBIBOLL数据不足"
    if price >= bu:
        return "sell", f"价格({price:.2f})突破上轨({bu:.2f})，大概率回调"
    if price <= bl:
        return "buy", f"价格({price:.2f})跌破下轨({bl:.2f})，大概率反弹"
    if price > bm:
        return "buy", f"价格({price:.2f})在BBI中轨({bm:.2f})上方，多方强势"
    return "sell", f"价格({price:.2f})在BBI中轨({bm:.2f})下方，空方强势"


def strategy_tower(ctx, params):
    tw = ctx["tower"].iloc[ctx["i"]]
    pre = ctx["tower"].iloc[ctx["i"] - 1] if ctx["i"] > 0 else 0
    if tw == 1 and pre == -1:
        return "buy", "宝塔线翻红，站上前收盘"
    if tw == -1 and pre == 1:
        return "sell", "宝塔线翻绿，跌破前收盘"
    if tw == 1:
        return "buy", "连续红柱，持仓"
    if tw == -1:
        return "sell", "连续绿柱，持续下跌"
    return "hold", "宝塔线走平"


def strategy_ma_combo(ctx, params):
    i = ctx["i"]
    price = ctx["price"]
    short = int(params.get("short", 5))
    mid = int(params.get("mid", 10))
    long = int(params.get("long", 60))
    ma_s = _ma_series(ctx["df"], short).iloc[i]
    ma_m = _ma_series(ctx["df"], mid).iloc[i]
    ma_l = _ma_series(ctx["df"], long).iloc[i]
    if pd.isna(ma_s) or pd.isna(ma_l):
        return "hold", "均线数据不足"
    if price > ma_s > ma_m > ma_l:
        return "buy", f"{short}日({ma_s:.2f})>{mid}日({ma_m:.2f})>{long}日({ma_l:.2f})，多头排列"
    if price < ma_s or price < ma_m:
        return "sell", f"价格({price:.2f})跌破MA{short}({ma_s:.2f})或MA{mid}({ma_m:.2f})"
    return "hold", "均线方向不明"


def strategy_two_line(ctx, params):
    short = int(params.get("short", 5))
    long = int(params.get("long", 10))
    ma_s = _ma_series(ctx["df"], short).iloc[ctx["i"]]
    ma_l = _ma_series(ctx["df"], long).iloc[ctx["i"]]
    if pd.isna(ma_s):
        return "hold", "数据不足"
    if ma_s > ma_l:
        return "buy", f"MA{short}({ma_s:.2f})>MA{long}({ma_l:.2f})，短线可操作"
    return "sell", f"MA{short}({ma_s:.2f})<MA{long}({ma_l:.2f})，清仓观望"


def strategy_life_line(ctx, params):
    period = int(params.get("period", 60))
    v = _ma_series(ctx["df"], period).iloc[ctx["i"]]
    if pd.isna(v):
        return "hold", f"MA{period}数据不足"
    if ctx["price"] > v:
        return "buy", f"价格({ctx['price']:.2f})在MA{period}({v:.2f})上方，积极做多"
    return "sell", f"价格({ctx['price']:.2f})在MA{period}({v:.2f})下方，空头市场"


def strategy_three_third(ctx, params):
    i = ctx["i"]
    price = ctx["price"]
    p1 = int(params.get("p1", 7))
    p2 = int(params.get("p2", 13))
    p3 = int(params.get("p3", 20))
    ma1 = _ma_series(ctx["df"], p1).iloc[i]
    ma2 = _ma_series(ctx["df"], p2).iloc[i]
    ma3 = _ma_series(ctx["df"], p3).iloc[i]
    if pd.isna(ma1) or pd.isna(ma3):
        return "hold", "数据不足"
    if price > ma1 and price > ma2 and price > ma3:
        return "buy", f"站上{p1}日({ma1:.2f})/{p2}日({ma2:.2f})/{p3}日({ma3:.2f})线"
    if price < ma1:
        return "sell", f"跌破{p1}日线({ma1:.2f})，分批减仓"
    return "hold", f"价格({price:.2f})在均线之间"


def strategy_sparrow(ctx, params):
    i = ctx["i"]
    close = ctx["close"]
    lookback = int(params.get("lookback", 5))
    target = float(params.get("target", 2.5))
    if i < lookback:
        return "hold", "数据不足"
    low = close.iloc[i - lookback : i].min()
    pnl = (ctx["price"] - low) / low * 100 if low > 0 else 0
    if pnl >= target:
        return "sell", f"自{lookback}日低点({low:.2f})已涨{pnl:.1f}%≥{target}%，见好就收"
    return "hold", f"自{lookback}日低点仅涨{pnl:.1f}%，未达{target}%止盈线"


def strategy_bounce(ctx, params):
    i = ctx["i"]
    close = ctx["close"]
    if i < 3:
        return "hold", "数据不足"
    rebound_pct = float(params.get("rebound_pct", 0.5))
    vol_increase = float(params.get("vol_increase", 20))
    pc = close.iloc[i - 1]
    pp = close.iloc[i - 2]
    dc = (pc - pp) / pp * 100
    if dc >= 0:
        return "hold", "昨日非下跌，无反弹条件"
    tc = (ctx["price"] - pc) / pc * 100
    vol_i = ctx["df"]["volume"].iloc[i]
    vol_p = ctx["df"]["volume"].iloc[i - 1]
    vr = (vol_i - vol_p) / vol_p * 100 if vol_p > 0 else 0
    if tc > abs(dc) * rebound_pct and vr > vol_increase:
        return "buy", f"涨幅{tc:.1f}%>昨日跌幅{abs(dc):.1f}%×{rebound_pct:.0%}，放量{vr:.0f}%"
    return "hold", f"反弹{tc:.1f}%未达条件或未放量"


def strategy_volume_divergence(ctx, params):
    close = ctx["close"]
    i = ctx["i"]
    lookback = int(params.get("lookback", 10))
    shrink = float(params.get("shrink", 0.7))
    expand = float(params.get("expand", 1.3))
    if i < lookback:
        return "hold", "数据不足"
    rh = close.iloc[i - lookback : i].max()
    vols = ctx["df"]["volume"].iloc[i - lookback : i]
    avg = vols.mean()
    vn = ctx["df"]["volume"].iloc[i]
    if ctx["price"] >= rh and vn < avg * shrink:
        return "sell", f"创{lookback}日新高但量萎缩，无量上涨警惕"
    if ctx["price"] >= rh and vn > avg * expand:
        return "buy", "放量突破，量价配合"
    return "hold", "无量价背离"


def strategy_resonance(ctx, params):
    diff, dea, _ = compute_macd(ctx["df"])
    k, d, _, _ = compute_kdj(ctx["df"])
    i = ctx["i"]
    if (
        diff.iloc[i] > dea.iloc[i]
        and diff.iloc[i - 1] <= dea.iloc[i - 1]
        and k.iloc[i] > d.iloc[i]
        and k.iloc[i - 1] <= d.iloc[i - 1]
        and ctx["price"] > ctx["boll_m"].iloc[i]
        and ctx["price"] > ctx["ma5"].iloc[i]
    ):
        return "buy", "MACD金叉+KDJ金叉+BOLL中轨+站上MA5"
    return "hold", "MACD+KDJ+BOLL+MA5未同向共振"


def strategy_dmi_psy(ctx, params):
    pdi_threshold = float(params.get("pdi_threshold", 5))
    psy_threshold = float(params.get("psy_threshold", 25))
    pdi, mdi, adx = compute_dmi(ctx["df"])
    psy = compute_psy(ctx["df"])
    pdi_v = pdi.iloc[ctx["i"]]
    psy_v = psy.iloc[ctx["i"]]
    if pd.isna(pdi_v) or pd.isna(psy_v):
        return "hold", "数据不足"
    if pdi_v < pdi_threshold and psy_v <= psy_threshold:
        return "buy", f"PDI({pdi_v:.1f})<{pdi_threshold}且PSY({psy_v:.0f})≤{psy_threshold}，超跌反弹"
    return "hold", f"PDI({pdi_v:.1f})/PSY({psy_v:.0f})未达超跌极值"


# ---------------- 自定义可视化规则策略 ----------------

CONDITION_METRIC_META = {
    "price_vs_ma5": "价格 vs MA5",
    "price_vs_ma10": "价格 vs MA10",
    "price_vs_ma20": "价格 vs MA20",
    "price_vs_ma60": "价格 vs MA60",
    "ma5_vs_ma10": "MA5 vs MA10",
    "ma5_vs_ma60": "MA5 vs MA60",
    "ma10_vs_ma60": "MA10 vs MA60",
    "macd_diff_vs_dea": "DIFF vs DEA",
    "macd_above_zero": "MACD零轴上",
    "macd_below_zero": "MACD零轴下",
    "k": "K值",
    "d": "D值",
    "j": "J值",
    "kdj_golden": "KDJ金叉",
    "kdj_death": "KDJ死叉",
    "price_in_boll_upper": "触及BOLL上轨",
    "price_in_boll_lower": "触及BOLL下轨",
    "psy_over": "PSY超买(≥75)",
    "psy_under": "PSY超卖(≤25)",
    "bias_over": "BIAS6超涨(≥3)",
    "bias_under": "BIAS6超跌(≤-3)",
    "pdi_vs_mdi": "PDI vs MDI",
    "sar_bull": "SAR翻红",
    "sar_bear": "SAR翻绿",
    "tower_red": "宝塔线红",
    "tower_green": "宝塔线绿",
    "close_above_open": "阳线收盘",
    "close_below_open": "阴线收盘",
    "volume_expand": "放量(>1.5倍)",
    "volume_shrink": "缩量(<0.7倍)",
}


def _eval_metric(ctx, metric):
    i = ctx["i"]
    df = ctx["df"]
    price = ctx["price"]
    if metric == "price_vs_ma5":
        return price - ctx["ma5"].iloc[i]
    if metric == "price_vs_ma10":
        return price - ctx["ma10"].iloc[i]
    if metric == "price_vs_ma20":
        return price - ctx["ma20"].iloc[i]
    if metric == "price_vs_ma60":
        return price - ctx["ma60"].iloc[i]
    if metric == "ma5_vs_ma10":
        return ctx["ma5"].iloc[i] - ctx["ma10"].iloc[i]
    if metric == "ma5_vs_ma60":
        return ctx["ma5"].iloc[i] - ctx["ma60"].iloc[i]
    if metric == "ma10_vs_ma60":
        return ctx["ma10"].iloc[i] - ctx["ma60"].iloc[i]
    if metric == "macd_diff_vs_dea":
        return ctx["macd_diff"].iloc[i] - ctx["macd_dea"].iloc[i]
    if metric == "macd_above_zero":
        return 1 if ctx["macd_diff"].iloc[i] > 0 else 0
    if metric == "macd_below_zero":
        return 1 if ctx["macd_diff"].iloc[i] < 0 else 0
    if metric == "k":
        return ctx["k"].iloc[i]
    if metric == "d":
        return ctx["d"].iloc[i]
    if metric == "j":
        return ctx["j"].iloc[i]
    if metric == "kdj_golden":
        return (
            1 if ctx["k"].iloc[i] > ctx["d"].iloc[i] and ctx["k"].iloc[i - 1] <= ctx["d"].iloc[i - 1] else 0
        )
    if metric == "kdj_death":
        return (
            1 if ctx["k"].iloc[i] < ctx["d"].iloc[i] and ctx["k"].iloc[i - 1] >= ctx["d"].iloc[i - 1] else 0
        )
    if metric == "price_in_boll_upper":
        return 1 if price >= ctx["boll_u"].iloc[i] else 0
    if metric == "price_in_boll_lower":
        return 1 if price <= ctx["boll_l"].iloc[i] else 0
    if metric == "psy_over":
        return 1 if ctx["psy"].iloc[i] >= 75 else 0
    if metric == "psy_under":
        return 1 if ctx["psy"].iloc[i] <= 25 else 0
    if metric == "bias_over":
        return 1 if ctx["bias1"].iloc[i] >= 3 else 0
    if metric == "bias_under":
        return 1 if ctx["bias1"].iloc[i] <= -3 else 0
    if metric == "pdi_vs_mdi":
        return ctx["pdi"].iloc[i] - ctx["mdi"].iloc[i]
    if metric == "sar_bull":
        return 1 if ctx["sar"][i] > 0 and price > ctx["sar"][i] else 0
    if metric == "sar_bear":
        return 1 if ctx["sar"][i] > 0 and price < ctx["sar"][i] else 0
    if metric == "tower_red":
        return 1 if ctx["tower"].iloc[i] > 0 else 0
    if metric == "tower_green":
        return 1 if ctx["tower"].iloc[i] < 0 else 0
    if metric == "close_above_open":
        return 1 if df["close"].iloc[i] > df["open"].iloc[i] else 0
    if metric == "close_below_open":
        return 1 if df["close"].iloc[i] < df["open"].iloc[i] else 0
    if metric == "volume_expand":
        v = df["volume"].iloc[i]
        avg = df["volume"].iloc[i - 5 : i].mean() if i >= 5 else df["volume"].mean()
        return 1 if avg > 0 and v > avg * 1.5 else 0
    if metric == "volume_shrink":
        v = df["volume"].iloc[i]
        avg = df["volume"].iloc[i - 5 : i].mean() if i >= 5 else df["volume"].mean()
        return 1 if avg > 0 and v < avg * 0.7 else 0
    return 0


def eval_condition(ctx, cond):
    """cond: {metric, op, threshold}  op in: >, >=, <, <=, ==, is_true"""
    metric = cond.get("metric")
    op = cond.get("op", ">")
    val = _eval_metric(ctx, metric)
    if pd.isna(val):
        return False
    threshold = cond.get("threshold", 0)
    if op == "is_true":
        return bool(val)
    try:
        if op == ">":
            return val > threshold
        if op == ">=":
            return val >= threshold
        if op == "<":
            return val < threshold
        if op == "<=":
            return val <= threshold
        if op == "==":
            return abs(val - threshold) < 1e-6
    except Exception:
        return False
    return False


def eval_custom_strategy(ctx, strat):
    """离线兜底：{name, buy:[conds], sell:[conds]} buy/sell 均为 AND 关系"""
    sig = "hold"
    reason = ""
    buy_conds = strat.get("buy", [])
    sell_conds = strat.get("sell", [])
    if buy_conds and all(eval_condition(ctx, c) for c in buy_conds):
        sig = "buy"
        reason = "买入条件满足: " + " 且 ".join(
            CONDITION_METRIC_META.get(c["metric"], c["metric"]) for c in buy_conds
        )
    elif sell_conds and all(eval_condition(ctx, c) for c in sell_conds):
        sig = "sell"
        reason = "卖出条件满足: " + " 且 ".join(
            CONDITION_METRIC_META.get(c["metric"], c["metric"]) for c in sell_conds
        )
    else:
        reason = "自定义条件未触发"
    return sig, reason


def format_indicators(ctx):
    i = ctx["i"]
    ind = ctx["indicators"]
    rt = ctx.get("realtime") or {}
    lines = [
        f"- 现价 {rt.get('price') or ctx['price']}，涨跌幅 {rt.get('pct', '-')}%",
        f"- MACD: DIFF {ind['macd_diff']}，DEA {ind['macd_dea']}，柱 {ind['macd_bar']}",
        f"- KDJ: K={ind['k']} D={ind['d']} J={ind['j']}",
        f"- BOLL(上/中/下): {ind['boll_u']}/{ind['boll_m']}/{ind['boll_l']}",
        f"- BBIBOLL(上/中/下): {ind['bbiboll_u']}/{ind['bbiboll_m']}/{ind['bbiboll_l']}",
        f"- 均线: MA5={ind['ma5']} MA10={ind['ma10']} MA20={ind['ma20']} MA60={ind['ma60']}",
        f"- PSY={ind['psy']}，BIAS6={ind['bias1']}% BIAS12={ind['bias2']}% BIAS24={ind['bias3']}%",
        f"- DMI: PDI={ind['pdi']} MDI={ind['mdi']} ADX={ind['adx']}",
        f"- SAR={ind['sar']}，宝塔线={'红' if ind['tower'] > 0 else ('绿' if ind['tower'] < 0 else '平')}",
    ]
    dfs = ctx.get("df")
    if dfs is not None and i > 0:
        cur_v = float(dfs["volume"].iloc[i])
        avg_v = float(dfs["volume"].iloc[max(0, i - 5) : i].mean())
        if avg_v and avg_v > 0:
            lines.append(f"- 今日成交量 {cur_v:.0f}，5日均量 {avg_v:.0f}，量比 {cur_v / avg_v:.2f}倍")
        else:
            lines.append(f"- 今日成交量 {cur_v:.0f}（5日均量不足）")
    return "\n".join(lines)


def judge_custom_with_ai(code, ctx, custom_strats, use_ai=True):
    """用 AI 判定自定义策略。返回 [{key,name,builtin:False,buy_rule,sell_rule,signal,reason,ai:bool}]"""
    if not custom_strats:
        return []
    today = datetime.now().strftime("%Y%m%d")
    out = []
    if use_ai:
        try:
            from ai_decider import AIDecider

            decider = AIDecider()
        except Exception:
            decider = None
    else:
        decider = None

    if decider is not None:
        cache_key = json.dumps(
            [
                {"id": s["id"], "n": s.get("name"), "b": s.get("buy_rule"), "s": s.get("sell_rule")}
                for s in custom_strats
            ],
            ensure_ascii=False,
        )
        # 先查整包缓存（所有自定义规则一次AI调用）
        cached_text = _ai_cache_get(code, today)
        results = None
        if cached_text:
            try:
                cache_payload = json.loads(cached_text)
                if cache_payload.get("rules") == cache_key and cache_payload.get("indicators") == hash(
                    ctx["i"]
                ):
                    results = [
                        {"id": r["id"], "signal": r["signal"], "reason": r["reason"] + "（当日缓存）"}
                        for r in cache_payload.get("results", [])
                    ]
            except Exception:
                results = None

        if results is None or len(results) != len(custom_strats):
            ind_text = format_indicators(ctx)
            try:
                resp = decider.judge_code(
                    code,
                    ctx.get("realtime") and ctx["realtime"].get("name") or code,
                    ind_text,
                    [
                        {
                            "id": s["id"],
                            "name": s.get("name", ""),
                            "buy_rule": s.get("buy_rule", ""),
                            "sell_rule": s.get("sell_rule", ""),
                        }
                        for s in custom_strats
                    ],
                )
            except Exception:
                resp = {"results": []}
            results = resp.get("results", [])
            # 仅当AI正常返回(未因限流/失败降级)才写缓存
            ok_results = [
                r
                for r in results
                if r.get("signal") in ("buy", "sell", "hold")
                and not str(r.get("reason", "")).startswith("AI判定不可用")
                and "AI解析失败" not in str(r.get("reason", ""))
            ]
            if len(ok_results) == len(custom_strats):
                try:
                    _ai_cache_set(
                        code,
                        today,
                        json.dumps(
                            {
                                "rules": cache_key,
                                "indicators": hash(ctx["i"]),
                                "results": [
                                    {"id": r["id"], "signal": r["signal"], "reason": r["reason"]}
                                    for r in results
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    )
                except Exception:
                    pass

        result_map = {r["id"]: r for r in results}
        for s in custom_strats:
            r = result_map.get(s["id"])
            if r and r.get("signal") in ("buy", "sell", "hold"):
                sig = r["signal"]
                reason = f"🤖AI: {r.get('reason', '')}"
            else:
                sig, reason = "hold", "AI未返回该规则结果"
            out.append(
                {
                    "key": s["id"],
                    "name": s.get("name", "自定义策略"),
                    "signal": sig,
                    "reason": reason,
                    "builtin": False,
                    "ai": True,
                    "buy_rule": s.get("buy_rule", ""),
                    "sell_rule": s.get("sell_rule", ""),
                }
            )
        return out

    # 无 AI → 离线兜底（旧条件 or 规则文本不可用则 hold）
    for s in custom_strats:
        if s.get("buy") or s.get("sell"):
            sg, rsn = eval_custom_strategy(ctx, s)
            reason = f"离线判定: {rsn}" if sg != "hold" else "AI不可用，离线规则未触发"
        else:
            sg, rsn = "hold", "AI不可用，无法按自然语言规则判定"
        out.append(
            {
                "key": s["id"],
                "name": s.get("name", "自定义策略"),
                "signal": sg,
                "reason": rsn,
                "builtin": False,
                "ai": False,
                "buy_rule": s.get("buy_rule", ""),
                "sell_rule": s.get("sell_rule", ""),
            }
        )
    return out


# ---------------- 主分析入口 ----------------


def analyze(code: str, use_ai: bool = True) -> dict:
    rt = fetch_realtime([code])
    realtime = rt[0] if rt else None
    df = get_daily_data(code)
    if len(df) < 30:
        return {"error": f"{code} 历史数据不足"}

    close = df["close"]
    i = len(df) - 1
    price = close.iloc[i]

    macd_diff, macd_dea, macd_bar = compute_macd(df)
    k, d, j, _ = compute_kdj(df)
    boll_u, boll_m, boll_l = compute_boll(df)
    psy = compute_psy(df)
    bias1, bias2, bias3 = compute_bias(df)
    pdi, mdi, adx = compute_dmi(df)
    sar, _trend = compute_sar(df)
    bbiboll_u, bbiboll_m, bbiboll_l = compute_bbiboll(df)
    tower = compute_tower(df)
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()
    df["ma7"] = close.rolling(7).mean()
    df["ma13"] = close.rolling(13).mean()

    indicators = {
        "macd_diff": round(macd_diff.iloc[i], 3),
        "macd_dea": round(macd_dea.iloc[i], 3),
        "macd_bar": round(macd_bar.iloc[i], 3),
        "k": round(k.iloc[i], 1),
        "d": round(d.iloc[i], 1),
        "j": round(j.iloc[i], 1),
        "boll_u": round(boll_u.iloc[i], 2),
        "boll_m": round(boll_m.iloc[i], 2),
        "boll_l": round(boll_l.iloc[i], 2),
        "bbiboll_u": round(bbiboll_u.iloc[i], 2),
        "bbiboll_m": round(bbiboll_m.iloc[i], 2),
        "bbiboll_l": round(bbiboll_l.iloc[i], 2),
        "ma5": round(df["ma5"].iloc[i], 2),
        "ma10": round(df["ma10"].iloc[i], 2),
        "ma20": round(df["ma20"].iloc[i], 2),
        "ma60": round(df["ma60"].iloc[i], 2),
        "psy": round(psy.iloc[i], 0),
        "bias1": round(bias1.iloc[i], 1),
        "bias2": round(bias2.iloc[i], 1),
        "bias3": round(bias3.iloc[i], 1),
        "pdi": round(pdi.iloc[i], 1),
        "mdi": round(mdi.iloc[i], 1),
        "adx": round(adx.iloc[i], 1),
        "sar": round(sar[i], 2) if sar[i] > 0 else 0,
        "tower": int(tower.iloc[i]) if pd.notna(tower.iloc[i]) else 0,
    }

    ctx = {
        "i": i,
        "price": price,
        "df": df,
        "close": close,
        "realtime": realtime,
        "indicators": indicators,
        "macd_diff": macd_diff,
        "macd_dea": macd_dea,
        "macd_bar": macd_bar,
        "k": k,
        "d": d,
        "j": j,
        "boll_u": boll_u,
        "boll_m": boll_m,
        "boll_l": boll_l,
        "psy": psy,
        "bias1": bias1,
        "bias2": bias2,
        "bias3": bias3,
        "pdi": pdi,
        "mdi": mdi,
        "adx": adx,
        "sar": sar,
        "bbiboll_u": bbiboll_u,
        "bbiboll_m": bbiboll_m,
        "bbiboll_l": bbiboll_l,
        "tower": tower,
        "ma5": df["ma5"],
        "ma10": df["ma10"],
        "ma20": df["ma20"],
        "ma60": df["ma60"],
        "ma7": df["ma7"],
        "ma13": df["ma13"],
    }

    BUILTIN = [
        ("macd", "MACD金叉死叉", strategy_macd),
        ("kdj", "KDJ超买超卖", strategy_kdj),
        ("ma_stop", "5日均线止损", strategy_ma_stop),
        ("boll", "BOLL布林线", strategy_boll),
        ("dmi", "DMI趋势", strategy_dmi),
        ("psy", "PSY心理线", strategy_psy),
        ("bias", "BIAS乖离率", strategy_bias),
        ("sar", "SAR止损", strategy_sar),
        ("bbiboll", "BBIBOLL多空布林", strategy_burnal),
        ("tower", "宝塔线TOWER", strategy_tower),
        ("ma_combo", "均线组合", strategy_ma_combo),
        ("two_line", "二线法", strategy_two_line),
        ("life_line", "60日生命线", strategy_life_line),
        ("three_third", "三分法", strategy_three_third),
        ("sparrow", "麻雀战术", strategy_sparrow),
        ("bounce", "反弹量化", strategy_bounce),
        ("volume_div", "量价背离", strategy_volume_divergence),
        ("resonance", "三指标共振", strategy_resonance),
        ("dmi_psy", "DMI+PSY超跌", strategy_dmi_psy),
    ]

    strategies_cfg = get_strategies()
    enabled_map = {}
    params_map = {}
    for s in strategies_cfg:
        enabled_map[s["id"]] = bool(s.get("enabled", True))
        params_map[s["id"]] = s.get("params", {})

    signals = []
    for sid, name, fn in BUILTIN:
        if sid in enabled_map and not enabled_map[sid]:
            continue
        params = {**DEFAULT_STRATEGY_PARAMS.get(sid, {}), **params_map.get(sid, {})}
        try:
            sg, rsn = fn(ctx, params)
        except Exception:
            log.exception("策略 %s(%s) 计算异常", sid, code)
            sg, rsn = "hold", "计算异常"
        signals.append({"key": sid, "name": name, "signal": sg, "reason": rsn, "builtin": True})

    custom_strats = [s for s in strategies_cfg if s.get("type") == "custom" and s.get("enabled", True)]
    custom_strats = migrate_custom_strategies(custom_strats)
    signals.extend(judge_custom_with_ai(code, ctx, custom_strats, use_ai=use_ai))

    buy_sigs = [s for s in signals if s["signal"] == "buy"]
    sell_sigs = [s for s in signals if s["signal"] == "sell"]
    hold_sigs = [s for s in signals if s["signal"] == "hold"]
    buy_n, sell_n, hold_n = len(buy_sigs), len(sell_sigs), len(hold_sigs)
    total_n = len(signals)

    # 动态阈值：方向票需达到启用策略的40%，且持有≥3票偏向（避免1票定方向）
    min_votes = max(3, int(total_n * 0.4))
    if buy_n >= min_votes and buy_n > sell_n:
        verdict, icon = "买入", "⬆"
    elif sell_n >= min_votes and sell_n > buy_n:
        verdict, icon = "卖出", "⬇"
    elif buy_n > sell_n and buy_n >= 3:
        verdict, icon = "买入", "⬆"
    elif sell_n > buy_n and sell_n >= 3:
        verdict, icon = "卖出", "⬇"
    else:
        verdict, icon = "观望", "⏸"

    kdf = df.tail(120)[["date", "open", "close", "high", "low", "volume"]].copy()
    kdf["date"] = kdf["date"].dt.strftime("%Y-%m-%d")
    kdf = kdf.astype({"open": float, "close": float, "high": float, "low": float, "volume": float})
    dates_tail = kdf["date"].tolist()

    def _tail(s):
        s = s.tail(120).reset_index(drop=True)
        return [None if pd.isna(v) else round(float(v), 3) for v in s]

    def _tail2(s):
        s = s.tail(120).reset_index(drop=True)
        return [None if pd.isna(v) else round(float(v), 2) for v in s]

    indicator_series = {
        "dates": dates_tail,
        "macd_diff": _tail(macd_diff),
        "macd_dea": _tail(macd_dea),
        "macd_bar": _tail(macd_bar),
        "k": _tail(k),
        "d": _tail(d),
        "j": _tail(j),
        "boll_u": _tail2(boll_u),
        "boll_m": _tail2(boll_m),
        "boll_l": _tail2(boll_l),
        "pdi": _tail(pdi),
        "mdi": _tail(mdi),
        "adx": _tail(adx),
        "psy": _tail(psy),
        "bias1": _tail(bias1),
        "bias2": _tail(bias2),
        "bias3": _tail(bias3),
        "ma5": _tail2(df["ma5"]),
        "ma10": _tail2(df["ma10"]),
        "ma20": _tail2(df["ma20"]),
        "ma60": _tail2(df["ma60"]),
        "bbiboll_u": _tail2(bbiboll_u),
        "bbiboll_m": _tail2(bbiboll_m),
        "bbiboll_l": _tail2(bbiboll_l),
    }

    return {
        "realtime": realtime,
        "indicators": indicators,
        "indicator_series": indicator_series,
        "signals": signals,
        "summary": {"buy": buy_n, "sell": sell_n, "hold": hold_n, "total": len(signals)},
        "verdict": verdict,
        "verdict_icon": icon,
        "buy_reasons": buy_sigs,
        "sell_reasons": sell_sigs,
        "hold_reasons": hold_sigs,
        "kline": kdf.to_dict("records"),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        res = analyze(sys.argv[1])
        if "error" in res:
            print(res["error"])
        else:
            print(
                f"\n{res['realtime']['name']} ({res['realtime']['code']}) {res['verdict']} {res['verdict_icon']}"
            )
            print(
                f"买入{res['summary']['buy']} | 卖出{res['summary']['sell']} | 观望{res['summary']['hold']} | 共{res['summary']['total']}"
            )
            for s in res["buy_reasons"][:5]:
                print(f"  [买] {s['name']}: {s['reason']}")
            for s in res["sell_reasons"][:5]:
                print(f"  [卖] {s['name']}: {s['reason']}")
