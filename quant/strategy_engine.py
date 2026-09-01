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


# 指标计算 + 默认参数提取到 strategy_indicators.py(纯计算,无副作用)
from strategy_indicators import (  # noqa: E402,F401  (re-export 保持兼容)
    DEFAULT_STRATEGY_PARAMS,
    compute_bbiboll,
    compute_bias,
    compute_boll,
    compute_dmi,
    compute_kdj,
    compute_macd,
    compute_mos_lows,
    compute_psy,
    compute_rsi,
    compute_sar,
    compute_tower,
)


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


# ---------------- 书2新增策略(操练大全独有) ----------------


def strategy_rsi(ctx, params):
    """RSI 相对强弱指标策略(操练大全8.5)。

    规则:
      - RSI<oversold(默认30)超卖 → 买
      - RSI>overbought(默认70)超买 → 卖
      - RSI 短线(p1=6)上穿长线(p2=12) 金叉 → 买
      - RSI 短线下穿长线 死叉 → 卖
    """
    p1 = int(params.get("p1", 6))
    p2 = int(params.get("p2", 12))
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    rsi1, rsi2 = compute_rsi(ctx["df"], p1=p1, p2=p2)
    i = ctx["i"]
    v1, v2 = rsi1.iloc[i], rsi2.iloc[i]
    if pd.isna(v1):
        return "hold", "RSI数据不足"
    # 金叉/死叉(RSI6 上穿/下穿 RSI12)
    golden = v1 > v2 and rsi1.iloc[i - 1] <= rsi2.iloc[i - 1]
    death = v1 < v2 and rsi1.iloc[i - 1] >= rsi2.iloc[i - 1]
    if v1 <= oversold:
        return "buy", f"RSI{p1}={v1:.1f}≤{oversold}超卖,有望反弹"
    if golden and v1 < 50:
        return "buy", f"RSI{p1}({v1:.1f})上穿RSI{p2}({v2:.1f})金叉,低位转强"
    if v1 >= overbought:
        return "sell", f"RSI{p1}={v1:.1f}≥{overbought}超买,获利盘多"
    if death and v1 > 50:
        return "sell", f"RSI{p1}({v1:.1f})下穿RSI{p2}({v2:.1f})死叉,高位转弱"
    if v1 > v2:
        return "buy", f"RSI{p1}({v1:.1f})>RSI{p2}({v2:.1f}),多头运行"
    return "sell", f"RSI{p1}({v1:.1f})<RSI{p2}({v2:.1f}),空头运行"


def strategy_bottom(ctx, params):
    """抄底策略(操练大全15章):缩量+大跌后加速下跌+MOS底背离复合形态。

    买入条件(满足任一):
      - 近 lookback 日已大跌(drop_pct)且当日缩量至均量 vol_shrink 倍以下(恐慌底)
      - MACD 底背离(MOS 低点,DI < DL前段)
    """
    lookback = int(params.get("lookback", 20))
    vol_shrink = float(params.get("vol_shrink", 0.5))
    drop_pct = float(params.get("drop_pct", -5))
    i = ctx["i"]
    close = ctx["close"]
    df = ctx["df"]
    if i < lookback:
        return "hold", "数据不足"
    # 条件1:近期已大跌+当日缩量
    ret = (close.iloc[i] - close.iloc[i - lookback]) / close.iloc[i - lookback] * 100
    avg_vol = df["volume"].iloc[i - lookback:i].mean()
    vol_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 1
    cond_drop = ret <= drop_pct and vol_ratio <= vol_shrink
    # 条件2:MOS底背离
    diff, dea, _ = compute_macd(df)
    mos = compute_mos_lows(df, diff, dea)
    cond_mos = mos.get("bottom", False)
    mos_msg = ""
    if cond_mos:
        mos_msg = f"MOS底背离(CL1={mos['cl1']:.2f}<CL2={mos['cl2']:.2f},DIFL1={mos['difl1']:.2f}≥DIFL2={mos['difl2']:.2f})"
    if cond_drop and cond_mos:
        return "buy", f"大跌后缩量+MOS底背离,复合抄底信号({ret:.1f}%/量比{vol_ratio:.2f})"
    if cond_drop:
        return "buy", f"{lookback}日跌{ret:.1f}%且缩量(量比{vol_ratio:.2f}≤{vol_shrink}),恐慌底"
    if cond_mos:
        return "buy", mos_msg
    return "hold", f"无抄底信号(近{lookback}日{ret:+.1f}%,量比{vol_ratio:.2f})"


def strategy_top(ctx, params):
    """逃顶策略(操练大全16章):天量+大涨后加速上涨复合形态。

    卖出条件(满足任一):
      - 近 lookback 日已大涨(rise_pct)且当日放量至均量 vol_expand 倍以上(天量见顶)
      - 价格创近 lookback 日新高但量能萎缩(量价背离,无量上涨)
    """
    lookback = int(params.get("lookback", 20))
    vol_expand = float(params.get("vol_expand", 2.0))
    rise_pct = float(params.get("rise_pct", 5))
    i = ctx["i"]
    close = ctx["close"]
    df = ctx["df"]
    if i < lookback:
        return "hold", "数据不足"
    ret = (close.iloc[i] - close.iloc[i - lookback]) / close.iloc[i - lookback] * 100
    avg_vol = df["volume"].iloc[i - lookback:i].mean()
    vol_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 1
    rh = close.iloc[i - lookback:i].max()
    new_high = close.iloc[i] >= rh
    # 条件1:大涨+天量
    cond_hot = ret >= rise_pct and vol_ratio >= vol_expand
    # 条件2:创近期新高但量能萎缩(量价背离)
    cond_div = new_high and vol_ratio < 0.7
    if cond_hot:
        return "sell", f"{lookback}日涨{ret:.1f}%且天量(量比{vol_ratio:.2f}≥{vol_expand}),见顶信号"
    if cond_div:
        return "sell", f"创{lookback}日新高但量萎缩(量比{vol_ratio:.2f}),无量上涨警惕"
    return "hold", f"无见顶信号(近{lookback}日{ret:+.1f}%,量比{vol_ratio:.2f})"


def strategy_zt(ctx, params):
    """涨停板策略(操练大全20章):涨停封板信号识别。

    买入条件:
      - 当日涨幅 ≥ zt_pct(默认9.6%,兼容主板10%/创业板20%由参数调整)
      - 当日量比 ≥ min_vol_ratio(放量封板,排除一字板无量特殊情况)
    """
    zt_pct = float(params.get("zt_pct", 9.6))
    min_vol_ratio = float(params.get("min_vol_ratio", 1.5))
    i = ctx["i"]
    df = ctx["df"]
    close = df["close"]
    if i < 1:
        return "hold", "数据不足"
    pct = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1] * 100
    avg_vol = df["volume"].iloc[max(0, i - 20):i].mean()
    vol_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    if pct >= 9.8:  # 创业板/科创板 20%涨停
        if vol_ratio >= min_vol_ratio:
            return "buy", f"涨停封板(+{pct:.1f}%,量比{vol_ratio:.1f}),强势追击"
        return "hold", f"涨停(+{pct:.1f}%)但量比{vol_ratio:.1f}不足,可能一字板"
    if pct >= zt_pct:
        if vol_ratio >= min_vol_ratio:
            return "buy", f"近涨停(+{pct:.1f}%≥{zt_pct}%,量比{vol_ratio:.1f}≥{min_vol_ratio}),封板强势"
        return "hold", f"近涨停(+{pct:.1f}%)但量比{vol_ratio:.1f}不足,封板不牢"
    if pct >= 5 and vol_ratio >= min_vol_ratio * 1.5:
        return "hold", f"大涨+{pct:.1f}%放量(量比{vol_ratio:.1f}),观望是否封板"
    return "hold", f"无涨停信号(+{pct:.1f}%,量比{vol_ratio:.1f})"


# ---------------- 操练大全12章 投资法则与策略 ----------------


def strategy_trend_follow(ctx, params):
    """顺势而为(操练大全12章):ADX 趋势强度 + 均线多头/空头排列复合判断。

    规则:
      - ADX>threshold(默认25) + MA5>MA10>MA20 多头排列 → buy
      - ADX>threshold + MA5<MA10<MA20 空头排列 → sell
      - ADX<weak(默认20) → hold(无明显趋势)
      - 否则按均线方向倾向
    """
    threshold = float(params.get("threshold", 25))
    weak = float(params.get("weak", 20))
    i = ctx["i"]
    if i < 60:
        return "hold", "数据不足"
    adx = ctx["adx"]
    ma5, ma10, ma20 = ctx["ma5"], ctx["ma10"], ctx["ma20"]
    a = adx.iloc[i]
    m5, m10, m20 = ma5.iloc[i], ma10.iloc[i], ma20.iloc[i]
    if pd.isna(a) or pd.isna(m20):
        return "hold", "指标数据不足"
    bull = m5 > m10 > m20
    bear = m5 < m10 < m20
    if a >= threshold and bull:
        return "buy", f"ADX={a:.1f}≥{threshold} 强趋势+均线多头排列(MA5>MA10>MA20),顺势做多"
    if a >= threshold and bear:
        return "sell", f"ADX={a:.1f}≥{threshold} 强趋势+均线空头排列(MA5<MA10<MA20),顺势做空"
    if a < weak:
        return "hold", f"ADX={a:.1f}<{weak} 无明显趋势,观望"
    if bull:
        return "buy", f"均线多头排列(MA5>MA10>MA20),ADX={a:.1f} 趋势走强"
    if bear:
        return "sell", f"均线空头排列(MA5<MA10<MA20),ADX={a:.1f} 趋势走弱"
    return "hold", f"ADX={a:.1f} 均线纠缠(MA5={m5:.2f}/MA10={m10:.2f}/MA20={m20:.2f}),方向不明"


def strategy_pyramid(ctx, params):
    """金字塔形买卖法(操练大全12章):以近 N 日均价为基准,阶梯加仓/减仓。

    规则:
      - 当前价 < 基准×(1-step) → buy(下跌加仓)
      - 当前价 > 基准×(1+step) → sell(上涨减仓)
      - step 控制阶梯宽度,默认 10%
    """
    n = int(params.get("n", 20))
    step = float(params.get("step", 0.1))
    i = ctx["i"]
    close = ctx["close"]
    if i < n:
        return "hold", "数据不足"
    base = close.iloc[i - n:i].mean()
    price = ctx["price"]
    if price < base * (1 - step):
        ret = (price - base) / base * 100
        return "buy", f"价{price:.2f}<均价{base:.2f}×(1-{step})={base*(1-step):.2f},低{ret:+.1f}%,金字塔加仓"
    if price > base * (1 + step):
        ret = (price - base) / base * 100
        return "sell", f"价{price:.2f}>均价{base:.2f}×(1+{step})={base*(1+step):.2f},高{ret:+.1f}%,金字塔减仓"
    return "hold", f"价{price:.2f} 在均价{base:.2f}±{step*100:.0f}%区间内,持仓观望"


def strategy_stop_profit(ctx, params):
    """有暴利便收手(操练大全12章):短期累计涨幅过大时主动止盈。

    规则:
      - 近 short 日(默认5)累计涨幅 ≥ short_pct(默认20) → sell
      - 近 long 日(默认10)累计涨幅 ≥ long_pct(默认30) → sell
      - 否则 hold
    """
    short = int(params.get("short", 5))
    long_ = int(params.get("long", 10))
    short_pct = float(params.get("short_pct", 20))
    long_pct = float(params.get("long_pct", 30))
    i = ctx["i"]
    close = ctx["close"]
    if i < long_:
        return "hold", "数据不足"
    ret_short = (close.iloc[i] - close.iloc[i - short]) / close.iloc[i - short] * 100
    ret_long = (close.iloc[i] - close.iloc[i - long_]) / close.iloc[i - long_] * 100
    if ret_long >= long_pct:
        return "sell", f"近{long_}日涨{ret_long:.1f}%≥{long_pct}%,暴利收手"
    if ret_short >= short_pct:
        return "sell", f"近{short}日涨{ret_short:.1f}%≥{short_pct}%,短期暴利止盈"
    return "hold", f"近{short}日{ret_short:+.1f}%/近{long_}日{ret_long:+.1f}%,未达止盈阈值"


def strategy_plan_trade(ctx, params):
    """计划你的交易(操练大全12章):进场后用 MA10 跟踪止损 + MACD 死叉复合。

    规则:
      - close > MA10 + MACD 多头(DIFF>DEA) → buy(趋势完好,持有/进场)
      - close < MA10 → sell(破位止损)
      - MACD 死叉(DIFF 下穿 DEA) → sell(趋势转折)
    """
    ma_period = int(params.get("ma_period", 10))
    i = ctx["i"]
    if i < max(ma_period, 35):
        return "hold", "数据不足"
    ma = ctx["ma10"] if ma_period == 10 else ctx["close"].rolling(ma_period).mean()
    price = ctx["price"]
    ma_v = ma.iloc[i]
    diff, dea = ctx["macd_diff"], ctx["macd_dea"]
    if pd.isna(ma_v):
        return "hold", "MA数据不足"
    death = diff.iloc[i] < dea.iloc[i] and diff.iloc[i - 1] >= dea.iloc[i - 1]
    if death:
        return "sell", f"MACD死叉(DIFF={diff.iloc[i]:.2f}下穿DEA={dea.iloc[i]:.2f}),趋势转折止损"
    if price < ma_v:
        return "sell", f"价{price:.2f}跌破MA{ma_period}({ma_v:.2f}),破位止损"
    if price > ma_v and diff.iloc[i] > dea.iloc[i]:
        return "buy", f"价{price:.2f}>MA{ma_period}({ma_v:.2f})+MACD多头,趋势完好"
    return "hold", f"价{price:.2f} vs MA{ma_period}({ma_v:.2f}),MACD未确认方向"


# ---------------- 漫画书 量能/实战战法 ----------------


def strategy_high_volume(ctx, params):
    """高量柱战法(漫画书):成交量突破 N 日最高量 + 价突破,放量启动信号。

    买入条件:
      - 当日量 > 近 N 日最高量(高量柱)
      - 当日 close > 近 N 日最高 close(突破前高)
    卖出条件:
      - 高量柱 + close < 近 N 日最低 close(放量破位)
    """
    n = int(params.get("n", 20))
    i = ctx["i"]
    df = ctx["df"]
    if i < n:
        return "hold", "数据不足"
    vol_max = df["volume"].iloc[i - n:i].max()
    close_max = ctx["close"].iloc[i - n:i].max()
    close_min = ctx["close"].iloc[i - n:i].min()
    v = df["volume"].iloc[i]
    c = ctx["close"].iloc[i]
    if v > vol_max and c > close_max:
        return "buy", f"高量柱(量{v/1e4:.0f}万>{n}日最高{vol_max/1e4:.0f}万)+突破前高{close_max:.2f},放量启动"
    if v > vol_max and c < close_min:
        return "sell", f"高量柱(量{v/1e4:.0f}万>{n}日最高{vol_max/1e4:.0f}万)+跌破前低{close_min:.2f},放量破位"
    return "hold", f"量{v/1e4:.0f}万/{n}日最高{vol_max/1e4:.0f}万,价{c:.2f}/{close_min:.2f}~{close_max:.2f}"


def strategy_demon_stock(ctx, params):
    """看妖股战法(漫画书):连续大涨识别妖股,启动期买入,过热期卖出。

    规则:
      - 近 consec 日(默认3)每日涨幅均 ≥ consec_pct(默认5%) → buy(启动期强势)
      - 近 hot 日(默认5)累计涨幅 ≥ hot_pct(默认30%) → sell(过热风险)
    """
    consec = int(params.get("consec", 3))
    consec_pct = float(params.get("consec_pct", 5))
    hot = int(params.get("hot", 5))
    hot_pct = float(params.get("hot_pct", 30))
    i = ctx["i"]
    close = ctx["close"]
    if i < max(consec, hot):
        return "hold", "数据不足"
    daily_ret = close.pct_change() * 100
    consec_strong = all(daily_ret.iloc[i - k] >= consec_pct for k in range(consec))
    if consec_strong:
        rets = [f"+{daily_ret.iloc[i-k]:.1f}%" for k in range(consec)]
        return "buy", f"近{consec}日连续大涨({'/'.join(rets[::-1])}),妖股启动期"
    ret_hot = (close.iloc[i] - close.iloc[i - hot]) / close.iloc[i - hot] * 100
    if ret_hot >= hot_pct:
        return "sell", f"近{hot}日累计涨{ret_hot:.1f}%≥{hot_pct}%,妖股过热,获利盘出逃风险"
    return "hold", f"近{hot}日{ret_hot:+.1f}%,无妖股特征"


def strategy_dragon_pullback(ctx, params):
    """龙回头战法(漫画书):前期涨停后回调到均线支撑 + 放量反弹二次启动。

    买入条件(全部满足):
      - 近 lookback 日(默认30)内出现过涨停(涨幅 ≥ zt_pct)
      - 当前回调到 MA10 附近(close 在 MA10 ± band% 内)
      - 当日放量反弹(量比 ≥ vol_ratio)
    """
    lookback = int(params.get("lookback", 30))
    zt_pct = float(params.get("zt_pct", 9.6))
    band = float(params.get("band", 3))
    vol_ratio_t = float(params.get("vol_ratio", 1.5))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < max(lookback, 60):
        return "hold", "数据不足"
    daily_ret = close.pct_change() * 100
    has_zt = any(daily_ret.iloc[i - k] >= zt_pct for k in range(1, lookback + 1))
    if not has_zt:
        return "hold", f"近{lookback}日无涨停,非龙回头"
    ma10 = ctx["ma10"].iloc[i]
    if pd.isna(ma10):
        return "hold", "MA10数据不足"
    price = ctx["price"]
    near_ma = abs(price - ma10) / ma10 * 100 <= band
    if not near_ma:
        return "hold", f"价{price:.2f}距MA10({ma10:.2f})超±{band}%,未回调到位"
    avg_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    if v_ratio >= vol_ratio_t and daily_ret.iloc[i] > 0:
        return "buy", f"前期涨停+回调到MA10({ma10:.2f},±{band}%)+放量反弹(量比{v_ratio:.1f}),龙回头买点"
    return "hold", f"已回调到MA10({ma10:.2f})但量比{v_ratio:.1f}(需≥{vol_ratio_t})或未反弹,等待确认"


def strategy_support_resistance(ctx, params):
    """压力支撑法(漫画书):突破近 N 日高点/跌破低点 + 放量确认。

    规则:
      - close > 近 N 日最高 + 量比 ≥ vol_ratio → buy(突破压力)
      - close < 近 N 日最低 + 量比 ≥ vol_ratio → sell(跌破支撑)
    """
    n = int(params.get("n", 20))
    vol_ratio_t = float(params.get("vol_ratio", 1.5))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < n:
        return "hold", "数据不足"
    hi = close.iloc[i - n:i].max()
    lo = close.iloc[i - n:i].min()
    price = ctx["price"]
    avg_vol = df["volume"].iloc[i - n:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 1
    if price > hi and v_ratio >= vol_ratio_t:
        return "buy", f"突破{n}日压力{hi:.2f}+放量(量比{v_ratio:.1f}),看涨"
    if price < lo and v_ratio >= vol_ratio_t:
        return "sell", f"跌破{n}日支撑{lo:.2f}+放量(量比{v_ratio:.1f}),看跌"
    return "hold", f"价{price:.2f}在支撑{lo:.2f}~压力{hi:.2f}区间内,量比{v_ratio:.1f}"


def strategy_range_trade(ctx, params):
    """区间交易法/地摊法(漫画书):在 N 日高低点区间内低买高卖。

    规则:
      - 价格 ≤ 支撑位(lo + (hi-lo)*low_pct) → buy
      - 价格 ≥ 压力位(hi - (hi-lo)*high_pct) → sell
    """
    n = int(params.get("n", 20))
    low_pct = float(params.get("low_pct", 0.2))
    high_pct = float(params.get("high_pct", 0.2))
    i = ctx["i"]
    close = ctx["close"]
    if i < n:
        return "hold", "数据不足"
    hi = close.iloc[i - n:i].max()
    lo = close.iloc[i - n:i].min()
    span = hi - lo
    if span <= 0:
        return "hold", "区间过窄,无法交易"
    support = lo + span * low_pct
    resistance = hi - span * high_pct
    price = ctx["price"]
    if price <= support:
        return "buy", f"价{price:.2f}≤支撑{support:.2f}(区间下{low_pct*100:.0f}%),地摊法低买"
    if price >= resistance:
        return "sell", f"价{price:.2f}≥压力{resistance:.2f}(区间上{high_pct*100:.0f}%),地摊法高卖"
    return "hold", f"价{price:.2f}在支撑{support:.2f}~压力{resistance:.2f}区间中段"


# ---------------- 操练大全15章 抄底策略 ----------------


def strategy_bottom_ma(ctx, params):
    """均线识底抄底(操练大全15章):均线多头排列确认底部。

    买入条件:
      - MA5 上穿 MA10(短期转强)
      - MA10 > MA20(中期支撑)
      - MA20 斜率向上(长期趋势)
    """
    i = ctx["i"]
    if i < 65:
        return "hold", "数据不足"
    ma5, ma10, ma20, ma60 = ctx["ma5"], ctx["ma10"], ctx["ma20"], ctx["ma60"]
    m5, m10, m20, m60 = ma5.iloc[i], ma10.iloc[i], ma20.iloc[i], ma60.iloc[i]
    if any(pd.isna(x) for x in (m5, m10, m20, m60)):
        return "hold", "均线数据不足"
    golden = m5 > m10 and ma5.iloc[i - 1] <= ma10.iloc[i - 1]
    ma20_slope = ma20.iloc[i - 5:i].mean() > ma20.iloc[i - 10:i].mean()
    if golden and m10 > m20 and ma20_slope:
        return "buy", f"MA5({m5:.2f})上穿MA10({m10:.2f})+MA10>MA20({m20:.2f})+MA20斜率向上,均线确认底"
    if m10 > m20 and ma20_slope and m5 > m10:
        return "buy", "均线多头排列(MA5>MA10>MA20)+MA20向上,底部形态"
    return "hold", f"MA5={m5:.2f}/MA10={m10:.2f}/MA20={m20:.2f},均线未确认底"


# ---------------- 操练大全16章 逃顶策略(周/月线) ----------------


def _resample_period(df, n):
    """从日 K 合成近 n 个交易日的周/月 K 线。"""
    recent = df.iloc[-n:]
    return {
        "open": recent["open"].iloc[0],
        "close": recent["close"].iloc[-1],
        "high": recent["high"].max(),
        "low": recent["low"].min(),
        "volume": recent["volume"].sum(),
    }


def strategy_top_weekly(ctx, params):
    """周线见顶(操练大全16章):周巨阳线 + 长上影见顶信号。

    卖出条件(同时满足):
      - 近 5 日(模拟一周)累计涨幅 ≥ rise_pct(默认 15%)
      - 上影线长度 > 实体长度(高位遇阻)
    """
    rise_pct = float(params.get("rise_pct", 15))
    i = ctx["i"]
    df = ctx["df"]
    if i < 5:
        return "hold", "数据不足"
    wk = _resample_period(df, 5)
    ret = (wk["close"] - wk["open"]) / wk["open"] * 100
    upper_shadow = wk["high"] - max(wk["close"], wk["open"])
    body = abs(wk["close"] - wk["open"])
    if ret >= rise_pct and upper_shadow > body and body > 0:
        return "sell", f"周涨{ret:.1f}%≥{rise_pct}%+长上影(上影{upper_shadow:.2f}>实体{body:.2f}),周线见顶"
    if ret >= rise_pct:
        return "sell", f"周涨{ret:.1f}%≥{rise_pct}%,周巨阳线警惕见顶"
    return "hold", f"本周涨{ret:+.1f}%,无周线见顶信号"


def strategy_top_monthly(ctx, params):
    """月线见顶(操练大全16章):月巨阳线 + 长上影见顶信号。

    卖出条件(同时满足):
      - 近 22 日(模拟一月)累计涨幅 ≥ rise_pct(默认 25%)
      - 上影线长度 > 实体长度
    """
    rise_pct = float(params.get("rise_pct", 25))
    i = ctx["i"]
    df = ctx["df"]
    if i < 22:
        return "hold", "数据不足"
    mo = _resample_period(df, 22)
    ret = (mo["close"] - mo["open"]) / mo["open"] * 100
    upper_shadow = mo["high"] - max(mo["close"], mo["open"])
    body = abs(mo["close"] - mo["open"])
    if ret >= rise_pct and upper_shadow > body and body > 0:
        return "sell", f"月涨{ret:.1f}%≥{rise_pct}%+长上影(上影{upper_shadow:.2f}>实体{body:.2f}),月线见顶"
    if ret >= rise_pct:
        return "sell", f"月涨{ret:.1f}%≥{rise_pct}%,月巨阳线警惕见顶"
    return "hold", f"本月涨{ret:+.1f}%,无月线见顶信号"


# ---------------- 操练大全17章 跟庄炒股 ----------------


def _percentile_pos(close, i, n, pct):
    """当前价在近 n 日价格分位(0~1,0=最低,1=最高)。"""
    if i < n:
        return 0.5
    window = close.iloc[i - n:i]
    lo, hi = window.min(), window.max()
    if hi == lo:
        return 0.5
    return (close.iloc[i] - lo) / (hi - lo)


def strategy_zhuang_test(ctx, params):
    """庄家试盘识别(操练大全17章):长上影 + 缩量 + 低位,庄家试探上方压力。

    识别条件(同时满足):
      - 上影线 > 实体 × shadow_ratio(默认2)
      - 当日量 < 5 日均量 × shrink(默认0.7)
      - 当前价在近 60 日 low_pct(默认30%)分位以下
    """
    shadow_ratio = float(params.get("shadow_ratio", 2))
    shrink = float(params.get("shrink", 0.7))
    low_pct = float(params.get("low_pct", 0.3))
    n = int(params.get("n", 60))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < max(n, 5):
        return "hold", "数据不足"
    o, c, h, _ = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
    upper_shadow = h - max(o, c)
    body = abs(c - o)
    if body <= 0:
        return "hold", "十字星,无实体"
    avg_vol = df["volume"].iloc[i - 5:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 1
    pos = _percentile_pos(close, i, n, low_pct)
    if upper_shadow > body * shadow_ratio and v_ratio < shrink and pos <= low_pct:
        return "hold", f"试盘:上影{upper_shadow:.2f}>实体{body:.2f}×{shadow_ratio}+缩量(量比{v_ratio:.1f})+低位(分位{pos*100:.0f}%),庄家试探"
    return "hold", "无试盘特征"


def strategy_zhuang_build(ctx, params):
    """庄家建仓识别(操练大全17章):低位 + 放量 + 横盘震荡吸筹。

    买入条件(同时满足):
      - 当前价在近 60 日 low_pct 分位以下(低位)
      - 近 5 日均量 > 近 20 日均量 × vol_ratio(放量)
      - 近 20 日振幅 < amplitude(默认 10%,横盘)
    """
    low_pct = float(params.get("low_pct", 0.3))
    vol_ratio_t = float(params.get("vol_ratio", 1.5))
    amplitude = float(params.get("amplitude", 10))
    n = int(params.get("n", 60))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < max(n, 20):
        return "hold", "数据不足"
    pos = _percentile_pos(close, i, n, low_pct)
    recent5_vol = df["volume"].iloc[i - 5:i].mean()
    recent20_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = recent5_vol / recent20_vol if recent20_vol > 0 else 1
    window = close.iloc[i - 20:i]
    amp = (window.max() - window.min()) / window.mean() * 100
    if pos <= low_pct and v_ratio >= vol_ratio_t and amp < amplitude:
        return "buy", f"建仓:低位(分位{pos*100:.0f}%)+放量(近5/20量比{v_ratio:.1f})+横盘(振幅{amp:.1f}%),庄家吸筹"
    return "hold", f"分位{pos*100:.0f}%/量比{v_ratio:.1f}/振幅{amp:.1f}%,无建仓特征"


def strategy_zhuang_pull(ctx, params):
    """庄家拉高识别(操练大全17章):放量突破 + 大阳线,庄家拉升。

    买入条件(同时满足):
      - 当日量 > 20 日均量 × vol_ratio(默认 2)
      - 当日涨幅 ≥ rise_pct(默认 5%)
      - close > 近 20 日最高(突破)
    """
    vol_ratio_t = float(params.get("vol_ratio", 2))
    rise_pct = float(params.get("rise_pct", 5))
    n = int(params.get("n", 20))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < n:
        return "hold", "数据不足"
    avg_vol = df["volume"].iloc[i - n:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    daily_ret = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1] * 100
    hi = close.iloc[i - n:i].max()
    if v_ratio >= vol_ratio_t and daily_ret >= rise_pct and close.iloc[i] > hi:
        return "buy", f"拉高:放量(量比{v_ratio:.1f})+涨{daily_ret:.1f}%+突破{n}日高{hi:.2f},庄家拉升"
    return "hold", f"量比{v_ratio:.1f}/涨{daily_ret:.1f}%/前高{hi:.2f},无拉高特征"


def strategy_zhuang_ship(ctx, params):
    """庄家出货识别(操练大全17章):高位 + 放量 + 滞涨。

    卖出条件(同时满足):
      - 当前价在近 60 日 high_pct(默认70%)分位以上(高位)
      - 近 5 日均量 > 近 20 日均量 × vol_ratio(放量)
      - 近 5 日累计涨幅 < stale_pct(默认 2%,滞涨)
    """
    high_pct = float(params.get("high_pct", 0.7))
    vol_ratio_t = float(params.get("vol_ratio", 1.5))
    stale_pct = float(params.get("stale_pct", 2))
    n = int(params.get("n", 60))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < max(n, 20):
        return "hold", "数据不足"
    pos = _percentile_pos(close, i, n, high_pct)
    recent5_vol = df["volume"].iloc[i - 5:i].mean()
    recent20_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = recent5_vol / recent20_vol if recent20_vol > 0 else 1
    recent5_ret = (close.iloc[i] - close.iloc[i - 5]) / close.iloc[i - 5] * 100
    if pos >= high_pct and v_ratio >= vol_ratio_t and recent5_ret < stale_pct:
        return "sell", f"出货:高位(分位{pos*100:.0f}%)+放量(量比{v_ratio:.1f})+滞涨(5日{recent5_ret:+.1f}%),庄家出货"
    return "hold", f"分位{pos*100:.0f}%/量比{v_ratio:.1f}/5日{recent5_ret:+.1f}%,无出货特征"


def strategy_zhuang_wash(ctx, params):
    """庄家洗盘识别(操练大全17章):缩量回调 + 不破 MA20 + 前期上涨。

    识别条件(同时满足):
      - 近 20 日累计涨幅 ≥ rise_pct(默认 10%,前期上涨)
      - 近 5 日缩量(量比 < shrink,默认 0.8)+ 跌幅在 pull_range(默认 -3%~-8%)
      - 当前价 > MA20(不破位)
    """
    rise_pct = float(params.get("rise_pct", 10))
    shrink = float(params.get("shrink", 0.8))
    pull_min = float(params.get("pull_min", -8))
    pull_max = float(params.get("pull_max", -3))
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < 20:
        return "hold", "数据不足"
    recent20_ret = (close.iloc[i] - close.iloc[i - 20]) / close.iloc[i - 20] * 100
    recent5_ret = (close.iloc[i] - close.iloc[i - 5]) / close.iloc[i - 5] * 100
    avg_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 1
    ma20 = ctx["ma20"].iloc[i]
    if pd.isna(ma20):
        return "hold", "MA20数据不足"
    if recent20_ret >= rise_pct and v_ratio < shrink and pull_min <= recent5_ret <= pull_max and close.iloc[i] > ma20:
        return "hold", f"洗盘:前期涨{recent20_ret:.1f}%+缩量回调(5日{recent5_ret:+.1f}%/量比{v_ratio:.1f})+不破MA20({ma20:.2f}),庄家洗盘"
    return "hold", f"20日涨{recent20_ret:.1f}%/5日{recent5_ret:+.1f}%/量比{v_ratio:.1f},无洗盘特征"


# ---------------- 操练大全20章 涨停板策略(细分) ----------------


def strategy_zt_type(ctx, params):
    """涨停板类型分类(操练大全20章):一字板/T字板/拉高板,根据强度给信号。

    规则(当日涨幅 ≥ zt_pct 默认 9.6%):
      - 一字板:open≈close≈high≈low(全天封板) → buy(最强势)
      - T 字板:open≈high≈close 但 low 明显低(开板后回封) → buy(强势)
      - 拉高板:open 未涨停,close 涨停(盘中拉至涨停) → hold(需次日确认)
    """
    zt_pct = float(params.get("zt_pct", 9.6))
    tolerance = float(params.get("tolerance", 0.5))
    i = ctx["i"]
    df = ctx["df"]
    if i < 1:
        return "hold", "数据不足"
    o, c, h, low = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
    prev_c = df["close"].iloc[i - 1]
    pct = (c - prev_c) / prev_c * 100
    if pct < zt_pct:
        return "hold", f"未涨停(涨{pct:.1f}%<{zt_pct}%)"
    zt_price = prev_c * (1 + zt_pct / 100)

    def _near(a, b, tol=tolerance):
        return abs(a - b) / b * 100 < tol

    if _near(o, zt_price) and _near(c, zt_price) and _near(h, zt_price) and _near(low, zt_price):
        return "buy", f"一字板(开/收/高/低≈涨停价{zt_price:.2f}),最强势"
    if _near(o, zt_price) and _near(c, zt_price) and not _near(low, zt_price):
        return "buy", f"T字板(开/收涨停但 low={low:.2f} 曾开板后回封),强势"
    return "hold", f"拉高板(开{o:.2f}未涨停+收{c:.2f}涨停),需次日确认"


def strategy_zt_unsealed(ctx, params):
    """涨停封不牢(操练大全20章):涨停但盘中开板,封板力度弱。

    卖出/观望条件:
      - 当日涨幅 ≥ zt_pct(默认 9.6%,涨停)
      - low < close × (1 - break_pct)(默认 1%,盘中开板)
      - 量比 ≥ vol_ratio(默认 2,放量)
    """
    zt_pct = float(params.get("zt_pct", 9.6))
    break_pct = float(params.get("break_pct", 1))
    vol_ratio_t = float(params.get("vol_ratio", 2))
    i = ctx["i"]
    df = ctx["df"]
    if i < 20:
        return "hold", "数据不足"
    c, low = df["close"].iloc[i], df["low"].iloc[i]
    prev_c = df["close"].iloc[i - 1]
    pct = (c - prev_c) / prev_c * 100
    if pct < zt_pct:
        return "hold", f"未涨停(涨{pct:.1f}%)"
    avg_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    opened = low < c * (1 - break_pct / 100)
    if opened and v_ratio >= vol_ratio_t:
        return "sell", f"涨停封不牢(涨停{pct:.1f}%+low{low:.2f}<收{c:.2f}×(1-{break_pct}%)+放量量比{v_ratio:.1f}),警惕开板"
    if opened:
        return "hold", f"涨停但盘中开板(low{low:.2f}<收{c:.2f}),量比{v_ratio:.1f}未放量,观望"
    return "hold", f"涨停封板稳(涨{pct:.1f}%+无开板),量比{v_ratio:.1f}"


def strategy_zt_pull(ctx, params):
    """拉高型涨停(操练大全20章):接近涨停但未封板+放量+大阳。

    规则(已融入 zt 主策略,此为细分):
      - 当日涨幅在 [pull_min, zt_pct)(默认 5~9.6%,接近涨停但未封)
      - 量比 ≥ vol_ratio(默认 2,放量)
      - 实体占比 ≥ body_ratio(默认 70%,大阳线)
    """
    zt_pct = float(params.get("zt_pct", 9.6))
    pull_min = float(params.get("pull_min", 5))
    vol_ratio_t = float(params.get("vol_ratio", 2))
    body_ratio_t = float(params.get("body_ratio", 70))
    i = ctx["i"]
    df = ctx["df"]
    if i < 20:
        return "hold", "数据不足"
    o, c, h, low = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
    prev_c = df["close"].iloc[i - 1]
    pct = (c - prev_c) / prev_c * 100
    avg_vol = df["volume"].iloc[i - 20:i].mean()
    v_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    body = abs(c - o)
    total = h - low
    body_pct = body / total * 100 if total > 0 else 0
    if pull_min <= pct < zt_pct and v_ratio >= vol_ratio_t and body_pct >= body_ratio_t:
        return "hold", f"拉高型涨停:涨{pct:.1f}%({pull_min}~{zt_pct}未封)+量比{v_ratio:.1f}+实体{body_pct:.0f}%,观望是否封板"
    return "hold", f"涨{pct:.1f}%/量比{v_ratio:.1f}/实体{body_pct:.0f}%,非拉高型"


# ---------------- 操练大全14章 选股策略(基本面) ----------------


def _fetch_finance_safe(code):
    """安全获取财务数据,失败返回 None。"""
    try:
        import stock_finance as sf

        return sf.fetch_finance(code)
    except Exception:
        return None


def strategy_pe_select(ctx, params):
    """市盈率选股(操练大全14章):基于 PE 估值筛选。

    规则:
      - PE < low_pe(默认15) → buy(低估值)
      - PE > high_pe(默认50) 或 PE<0(亏损) → sell(高估或亏损)
      - 否则 hold
      - 数据不可得 → hold(数据不足)
    """
    low_pe = float(params.get("low_pe", 15))
    high_pe = float(params.get("high_pe", 50))
    code = ctx.get("code", "")
    if not code:
        return "hold", "无股票代码"
    fin = _fetch_finance_safe(code)
    if not fin:
        return "hold", "财务数据不可得"
    pe = fin.get("pe_ttm")
    if pe is None or pe == "-" or pe == "":
        return "hold", "PE 数据缺失"
    try:
        pe = float(pe)
    except (TypeError, ValueError):
        return "hold", "PE 数据无效"
    if pe < 0:
        return "sell", f"PE={pe:.1f}<0 亏损,回避"
    if pe < low_pe:
        return "buy", f"PE={pe:.1f}<{low_pe} 低估值"
    if pe > high_pe:
        return "sell", f"PE={pe:.1f}>{high_pe} 高估值"
    return "hold", f"PE={pe:.1f} 估值合理区间({low_pe}~{high_pe})"


def strategy_roe_pe(ctx, params):
    """ROE+PE 复合选股(漫画书 roa_pe_筹码 的可实现部分):优质 + 低估。

    买入条件(同时满足):
      - ROE ≥ roe_min(默认15%)
      - PE < pe_max(默认25)
    卖出条件:
      - ROE < roe_bad(默认5%) + PE > pe_high(默认50) → sell(差高估)
    """
    roe_min = float(params.get("roe_min", 15))
    pe_max = float(params.get("pe_max", 25))
    roe_bad = float(params.get("roe_bad", 5))
    pe_high = float(params.get("pe_high", 50))
    code = ctx.get("code", "")
    if not code:
        return "hold", "无股票代码"
    fin = _fetch_finance_safe(code)
    if not fin:
        return "hold", "财务数据不可得"
    pe = fin.get("pe_ttm")
    roe = fin.get("roe")
    try:
        pe = float(pe) if pe not in (None, "-", "") else None
    except (TypeError, ValueError):
        pe = None
    try:
        roe = float(roe) if roe not in (None, "-", "") else None
    except (TypeError, ValueError):
        roe = None
    if pe is None or roe is None:
        return "hold", f"数据缺失(PE={pe},ROE={roe})"
    if roe >= roe_min and pe > 0 and pe < pe_max:
        return "buy", f"ROE={roe:.1f}%≥{roe_min}%+PE={pe:.1f}<{pe_max},优质低估"
    if roe < roe_bad and pe > pe_high:
        return "sell", f"ROE={roe:.1f}%<{roe_bad}%+PE={pe:.1f}>{pe_high},差高估"
    return "hold", f"ROE={roe:.1f}%/PE={pe:.1f},未达优质低估阈值"


# ---------------- 漫画书 实战战法(剩余) ----------------


def strategy_daban(ctx, params):
    """打板策略(漫画书实战战法):日K线简化版,连板追击。

    与 zt 区别:zt 只看当日封板,daban 要求连板,更强势。

    买入条件(同时满足):
      - 当日涨幅 >= zt_pct(默认 9.6%)
      - 当日量比 >= min_vol_ratio(默认 1.5)
      - 近 N 日(默认 10)连板数 >= consec(默认 2)
    """
    zt_pct = float(params.get("zt_pct", 9.6))
    min_vol_ratio = float(params.get("min_vol_ratio", 1.5))
    consec = int(params.get("consec", 2))
    n = int(params.get("n", 10))
    i = ctx["i"]
    df = ctx["df"]
    close = df["close"]
    if i < n + 1:
        return "hold", "数据不足"
    pct = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1] * 100
    avg_vol = df["volume"].iloc[max(0, i - 20):i].mean()
    vol_ratio = df["volume"].iloc[i] / avg_vol if avg_vol > 0 else 0
    # 连板数(从今日往前数,中断即停)
    zt_count = 0
    for j in range(i, max(i - n, 0), -1):
        if j < 1:
            break
        p = (close.iloc[j] - close.iloc[j - 1]) / close.iloc[j - 1] * 100
        if p >= zt_pct - 0.1:
            zt_count += 1
        else:
            break
    if pct >= zt_pct and vol_ratio >= min_vol_ratio and zt_count >= consec:
        return "buy", f"打板:今+{pct:.1f}%+量比{vol_ratio:.1f}+连板{zt_count}日,强势追击"
    if pct >= zt_pct and zt_count >= consec:
        return "hold", f"涨停+{pct:.1f}%连板{zt_count}日,但量比{vol_ratio:.1f}不足"
    return "hold", f"无打板信号(今+{pct:.1f}%,连板{zt_count}日,量比{vol_ratio:.1f})"


def strategy_fupan(ctx, params):
    """复盘法(漫画书实战战法):量化版,统计近 N 日 K 线特征综合判断。

    规则:
      - 统计近 N 日(默认 30)涨跌停次数、累计涨幅、关键支撑压力位
      - buy: 近期有涨停 + 当前在支撑位(近 N 日低点±support_pct%) + 缩量回调(量比<1)
      - sell: 近期累计涨幅>=rise_threshold + 当前在压力位(近 N 日高点±resistance_pct%) + 放量滞涨(量比>=1.5 且 当日|涨跌|<2%)
      - 否则 hold
    """
    n = int(params.get("n", 30))
    zt_pct = float(params.get("zt_pct", 9.6))
    support_pct = float(params.get("support_pct", 5))
    resistance_pct = float(params.get("resistance_pct", 5))
    rise_threshold = float(params.get("rise_threshold", 20))
    i = ctx["i"]
    df = ctx["df"]
    close = df["close"]
    vol = df["volume"]
    if i < n + 1:
        return "hold", "数据不足"
    recent_close = close.iloc[i - n:i + 1]
    recent_vol = vol.iloc[i - n:i + 1]
    recent_high = float(recent_close.max())
    recent_low = float(recent_close.min())
    # 涨停次数
    zt_count = 0
    for j in range(i, max(i - n, 0), -1):
        if j < 1:
            break
        p = (close.iloc[j] - close.iloc[j - 1]) / close.iloc[j - 1] * 100
        if p >= zt_pct - 0.1:
            zt_count += 1
    cum_pct = (close.iloc[i] - close.iloc[i - n]) / close.iloc[i - n] * 100
    price = ctx["price"]
    near_low = abs(price - recent_low) / recent_low * 100 <= support_pct
    near_high = abs(price - recent_high) / recent_high * 100 <= resistance_pct
    avg_vol = float(recent_vol.iloc[:-1].mean())
    vol_ratio = float(recent_vol.iloc[-1]) / avg_vol if avg_vol > 0 else 0
    today_pct = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1] * 100
    if zt_count >= 1 and near_low and vol_ratio < 1:
        return "buy", f"复盘:近{n}日{zt_count}次涨停+支撑位{recent_low:.2f}附近+缩量(量比{vol_ratio:.1f}),低吸"
    if cum_pct >= rise_threshold and near_high and vol_ratio >= 1.5 and abs(today_pct) < 2:
        return "sell", f"复盘:近{n}日累计+{cum_pct:.1f}%+压力位{recent_high:.2f}附近+放量滞涨(量比{vol_ratio:.1f}),减仓"
    return "hold", f"复盘:近{n}日{zt_count}次涨停+累计{cum_pct:+.1f}%+量比{vol_ratio:.1f},无信号"


# ---------------- 操练大全15章 抄底(剩余) ----------------


def strategy_bottom_time(ctx, params):
    """利用时间识底(操练大全15章):斐波那契时间窗。

    规则:
      - 找近 N 日(默认 120)最低点位置
      - 从最低点算起,经过斐波那契时间窗 8/13/21/34/55/89 日
      - 当前距时间窗 ±tolerance(默认 2)日 + 价在低位区(<=MA60) → buy(时间共振)
      - 当前距时间窗 ±tolerance 日 + 价在高位区(>=MA60×1.1) → sell
    """
    n = int(params.get("n", 120))
    tolerance = int(params.get("tolerance", 2))
    i = ctx["i"]
    df = ctx["df"]
    close = df["close"]
    if i < n:
        return "hold", "数据不足"
    recent = close.iloc[i - n + 1:i + 1]
    low_idx = int(recent.idxmin())
    low_offset = i - low_idx
    fib_windows = [8, 13, 21, 34, 55, 89]
    near_window = None
    for w in fib_windows:
        if abs(low_offset - w) <= tolerance:
            near_window = w
            break
    if near_window is None:
        return "hold", f"距最低点{low_offset}日,不在斐波那契窗(8/13/21/34/55/89±{tolerance})"
    ma60 = ctx["ma60"]
    if pd.isna(ma60.iloc[i]):
        return "hold", "MA60 数据不足"
    price = ctx["price"]
    ma60_val = float(ma60.iloc[i])
    if price <= ma60_val:
        return "buy", f"时间窗:距最低点{low_offset}日≈斐波那契{near_window}日+价{price:.2f}<=MA60({ma60_val:.2f}),时间共振低吸"
    if price >= ma60_val * 1.1:
        return "sell", f"时间窗:距最低点{low_offset}日≈斐波那契{near_window}日+价{price:.2f}>MA60×1.1({ma60_val * 1.1:.2f}),时间共振高抛"
    return "hold", f"时间窗:距最低点{low_offset}日≈斐波那契{near_window}日,价{price:.2f}在MA60附近,观望"


# ---------------- 操练大全14章 选股(剩余) ----------------


def strategy_shareholder_select(ctx, params):
    """股东人数变动选股(操练大全14章):筹码集中/分散趋势。

    规则:
      - 调东财 datacenter 接口取最近 2 期股东人数
      - change_pct <= concentrate(默认 -5) + 价在低位区(60日分位<=0.3) → buy(筹码集中)
      - change_pct >= disperse(默认 10) + 价在高位区(60日分位>=0.7) → sell(筹码分散)
      - 数据不可得 → hold
    """
    concentrate = float(params.get("concentrate", -5))
    disperse = float(params.get("disperse", 10))
    code = ctx.get("code", "")
    if not code:
        return "hold", "无股票代码"
    try:
        import stock_finance as sf

        data = sf.fetch_shareholder(code)
    except Exception:
        return "hold", "股东人数数据不可得"
    if "error" in data or data.get("change_pct") is None:
        return "hold", "股东人数数据不可得"
    change_pct = float(data["change_pct"])
    hold_focus = data.get("hold_focus", "")
    i = ctx["i"]
    close = ctx["close"]
    if i < 60:
        return "hold", "数据不足"
    pos = _percentile_pos(close, i, 60, 0.3)  # 0~1
    if change_pct <= concentrate and pos <= 0.3:
        return "buy", f"股东数{change_pct:+.1f}%<= {concentrate}%(集中,{hold_focus})+价在低位(分位{pos:.1%}),低吸"
    if change_pct >= disperse and pos >= 0.7:
        return "sell", f"股东数{change_pct:+.1f}%>= {disperse}%(分散,{hold_focus})+价在高位(分位{pos:.1%}),减仓"
    return "hold", f"股东数{change_pct:+.1f}%({hold_focus})+价在分位{pos:.1%},无信号"


# 政策利好/利空关键词(用于 policy_select 关键词匹配,避免依赖 LLM)
_POLICY_POSITIVE_KEYWORDS = (
    "支持", "扶持", "利好", "政策", "补贴", "获批", "中标", "增持", "回购",
    "增长", "超预期", "突破", "创新高", "盈利", "分红", "纳入", "试点",
    "战略", "规划", "投资", "签约", "合作", "订单", "量产", "上线",
)
_POLICY_NEGATIVE_KEYWORDS = (
    "处罚", "违规", "减持", "退市", "亏损", "下滑", "质疑", "调查", "警示",
    "停牌", "风险", "爆雷", "诉讼", "仲裁", "质押", "违约", "下调",
    "警示函", "监管", "立案", "问询", "被列",
)


def strategy_policy_select(ctx, params):
    """国家经济政策选股(操练大全14章):新闻关键词匹配版。

    规则:
      - 调 news_digest.fetch_stock_news 取该股近 N 条新闻(默认 10)
      - 统计利好/利空关键词命中次数
      - pos_hits >= min_positive(默认 2) + 价在低位区 → buy
      - neg_hits >= min_negative(默认 2) + 价在高位区 → sell
      - 无新闻或命中不足 → hold
    """
    num = int(params.get("num", 10))
    min_positive = int(params.get("min_positive", 2))
    min_negative = int(params.get("min_negative", 2))
    code = ctx.get("code", "")
    if not code:
        return "hold", "无股票代码"
    try:
        import news_digest

        news = news_digest.fetch_stock_news(code, num=num, strict=True)
    except Exception:
        return "hold", "新闻数据不可得"
    if not news:
        return "hold", "无相关新闻"
    pos_hits = 0
    neg_hits = 0
    pos_titles = []
    neg_titles = []
    for n_item in news:
        text = (n_item.get("title", "") + " " + n_item.get("summary", "")).lower()
        for kw in _POLICY_POSITIVE_KEYWORDS:
            if kw in text:
                pos_hits += 1
                pos_titles.append(n_item.get("title", "")[:30])
                break
        for kw in _POLICY_NEGATIVE_KEYWORDS:
            if kw in text:
                neg_hits += 1
                neg_titles.append(n_item.get("title", "")[:30])
                break
    i = ctx["i"]
    close = ctx["close"]
    if i < 60:
        return "hold", "数据不足"
    pos = _percentile_pos(close, i, 60, 0.3)
    if pos_hits >= min_positive and pos <= 0.3:
        sample = pos_titles[0] if pos_titles else ""
        return "buy", f"政策利好:近{len(news)}条新闻{pos_hits}条利好+价在低位(分位{pos:.1%}),例:{sample}"
    if neg_hits >= min_negative and pos >= 0.7:
        sample = neg_titles[0] if neg_titles else ""
        return "sell", f"政策利空:近{len(news)}条新闻{neg_hits}条利空+价在高位(分位{pos:.1%}),例:{sample}"
    return "hold", f"近{len(news)}条新闻:利好{pos_hits}/利空{neg_hits}+价在分位{pos:.1%},无明显政策面信号"


# ---------------- 经典 K 线形态 + 顶背离 + 缺口(补齐) ----------------


def _kline_body(df, i):
    """返回 (实体大小, 上下影长度, 是否阳线) 给定 K 线 index。"""
    o, c, h, lo = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
    body = abs(c - o)
    is_bull = c > o
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - lo
    return body, upper_shadow, lower_shadow, is_bull


def strategy_kline_pattern(ctx, params):
    """K线形态识别(经典日本蜡烛图):识别多种经典反转/中继形态。

    覆盖形态:
      - 早晨之星 / 黄昏之星(3 K 线反转)
      - 锤头 / 流星(单 K 长下影/上影)
      - 看涨吞没 / 看跌吞没(2 K 线反包)
      - 十字星(开≈收)
      - 红三兵 / 黑三兵(3 连阳/阴)
      - 孕线(今日实体 << 昨日实体)

    规则:
      - 识别当前形态,看涨形态 + 价在低位区(60日分位<=0.4)→ buy
      - 看跌形态 + 价在高位区(60日分位>=0.6)→ sell
      - 中性形态(十字星/孕线) + 低位 → 倾向 buy
      - 无明显形态 → hold
    """
    i = ctx["i"]
    df = ctx["df"]
    close = ctx["close"]
    if i < 3:
        return "hold", "数据不足"
    body, upper, lower, is_bull = _kline_body(df, i)
    body_prev, _, _, is_bull_prev = _kline_body(df, i - 1)
    body_p2, _, _, is_bull_p2 = _kline_body(df, i - 2)
    avg_body = (body_prev + body_p2) / 2 if (body_prev + body_p2) > 0 else 0.01
    # 识别形态
    pattern = None
    signal_dir = None  # 'bull' / 'bear' / 'neutral'
    # 十字星(开≈收,实体极小)
    if body < avg_body * 0.3:
        pattern = "十字星"
        signal_dir = "neutral"
    # 孕线(今日实体 < 昨日 50%)
    elif body_prev > 0 and body < body_prev * 0.5:
        pattern = "孕线"
        signal_dir = "neutral"
    # 看涨吞没(今日大阳包昨日阴)
    elif is_bull and not is_bull_prev and body > body_prev and \
            df["close"].iloc[i] >= df["open"].iloc[i - 1] and df["open"].iloc[i] <= df["close"].iloc[i - 1]:
        pattern = "看涨吞没"
        signal_dir = "bull"
    # 看跌吞没
    elif not is_bull and is_bull_prev and body > body_prev and \
            df["open"].iloc[i] >= df["close"].iloc[i - 1] and df["close"].iloc[i] <= df["open"].iloc[i - 1]:
        pattern = "看跌吞没"
        signal_dir = "bear"
    # 锤头(小实体在上,长下影 >= 2× 实体)
    elif is_bull and lower >= body * 2 and upper < body:
        pattern = "锤头"
        signal_dir = "bull"
    # 流星(小实体在下,长上影 >= 2× 实体)
    elif not is_bull and upper >= body * 2 and lower < body:
        pattern = "流星"
        signal_dir = "bear"
    # 早晨之星(3 K:大阴 + 跳空小实体 + 大阳收回 50% 以上)
    elif not is_bull_p2 and body_p2 > avg_body and body_prev < body_p2 * 0.5 and \
            is_bull and body > body_prev and \
            df["close"].iloc[i] > (df["open"].iloc[i - 2] + df["close"].iloc[i - 2]) / 2:
        pattern = "早晨之星"
        signal_dir = "bull"
    # 黄昏之星
    elif is_bull_p2 and body_p2 > avg_body and body_prev < body_p2 * 0.5 and \
            not is_bull and body > body_prev and \
            df["close"].iloc[i] < (df["open"].iloc[i - 2] + df["close"].iloc[i - 2]) / 2:
        pattern = "黄昏之星"
        signal_dir = "bear"
    # 红三兵
    elif is_bull and is_bull_prev and is_bull_p2 and \
            df["close"].iloc[i] > df["close"].iloc[i - 1] > df["close"].iloc[i - 2]:
        pattern = "红三兵"
        signal_dir = "bull"
    # 黑三兵
    elif not is_bull and not is_bull_prev and not is_bull_p2 and \
            df["close"].iloc[i] < df["close"].iloc[i - 1] < df["close"].iloc[i - 2]:
        pattern = "黑三兵"
        signal_dir = "bear"
    if pattern is None:
        return "hold", "无明显 K 线形态"
    # 结合价位分位判断
    if i < 60:
        return "hold", f"识别到 {pattern},但数据不足判断价位"
    pos = _percentile_pos(close, i, 60, 0.4)
    if signal_dir == "bull" and pos <= 0.4:
        return "buy", f"K线形态 {pattern}(看涨)+ 价在低位(分位{pos:.1%}),低吸"
    if signal_dir == "bear" and pos >= 0.6:
        return "sell", f"K线形态 {pattern}(看跌)+ 价在高位(分位{pos:.1%}),减仓"
    if signal_dir == "neutral" and pos <= 0.4:
        return "buy", f"K线形态 {pattern}(中性)+ 价在低位(分位{pos:.1%}),倾向低吸"
    if signal_dir == "neutral" and pos >= 0.6:
        return "sell", f"K线形态 {pattern}(中性)+ 价在高位(分位{pos:.1%}),倾向减仓"
    return "hold", f"K线形态 {pattern}({signal_dir}),价在分位{pos:.1%},等位置确认"


def _find_top_divergence(close, indicator, n=60, lookback=5):
    """通用顶背离识别:找近 n 日指标的最近两个高点,判断是否背离。

    顶背离 = 价格创新高 + 指标高点下降
    Args:
        close: 收盘价 Series
        indicator: 指标 Series(DIF / RSI 等)
        n: 回看窗口
        lookback: 当前高点相对最近高点的容忍天数
    Returns: (是否背离, 当前价, 前高价, 当前指标值, 前高指标值)
    """
    if len(close) < n:
        return False, None, None, None, None
    window_close = close.iloc[-n:]
    window_ind = indicator.iloc[-n:]
    # 找局部高点:在窗口内找指标最大值的位置
    # 简化:找指标最高的两个不连续点(间距>=5日)
    ind_arr = window_ind.values
    close_arr = window_close.values
    # 找指标最大值位置
    if len(ind_arr) < 10:
        return False, None, None, None, None
    # 第一个高点:窗口内指标最大
    idx1 = int(np.argmax(ind_arr))
    # 在 idx1 之前找第二个高点
    if idx1 < 5:
        return False, None, None, None, None
    idx0 = int(np.argmax(ind_arr[:idx1 - 5]))
    # 顶背离:price[idx1] > price[idx0] 但 indicator[idx1] < indicator[idx0]
    if close_arr[idx1] > close_arr[idx0] and ind_arr[idx1] < ind_arr[idx0]:
        return True, float(close_arr[idx1]), float(close_arr[idx0]), float(ind_arr[idx1]), float(ind_arr[idx0])
    return False, float(close_arr[idx1]), float(close_arr[idx0]), float(ind_arr[idx1]), float(ind_arr[idx0])


def strategy_macd_top_divergence(ctx, params):
    """MACD 顶背离识别(经典技术分析):价格新高但 MACD DIF 未新高。

    规则:
      - 找近 N 日(默认 60)DIF 的最近两个高点
      - 价格新高 + DIF 高点下降 → 顶背离 → sell(高位风险)
      - 价格新高 + DIF 也新高 → 趋势健康 → hold
    """
    n = int(params.get("n", 60))
    i = ctx["i"]
    if i < n:
        return "hold", "数据不足"
    diff = ctx["macd_diff"]
    close = ctx["close"]
    div, p1, p0, d1, d0 = _find_top_divergence(close, diff, n=n)
    if p1 is None:
        return "hold", "无足够高点对比"
    if div:
        return "sell", f"MACD顶背离:价{p1:.2f}>{p0:.2f}但DIF{d1:.2f}<{d0:.2f},高位风险"
    return "hold", f"无顶背离:价{p1:.2f}/DIF{d1:.2f}(前高{p0:.2f}/DIF{d0:.2f}),趋势健康"


def strategy_rsi_top_divergence(ctx, params):
    """RSI 顶背离识别:价格新高但 RSI 未新高。

    规则:
      - 找近 N 日(默认 60)RSI 的最近两个高点
      - 价格新高 + RSI 高点下降 → 顶背离 → sell(超买衰竭)
      - 价格新高 + RSI 也新高 → 趋势健康 → hold
    """
    n = int(params.get("n", 60))
    i = ctx["i"]
    if i < n:
        return "hold", "数据不足"
    rsi = ctx.get("rsi6")
    if rsi is None:
        return "hold", "RSI 数据缺失"
    close = ctx["close"]
    div, p1, p0, r1, r0 = _find_top_divergence(close, rsi, n=n)
    if p1 is None:
        return "hold", "无足够高点对比"
    if div:
        return "sell", f"RSI顶背离:价{p1:.2f}>{p0:.2f}但RSI{r1:.1f}<{r0:.1f},超买衰竭"
    return "hold", f"无RSI顶背离:价{p1:.2f}/RSI{r1:.1f}(前高{p0:.2f}/RSI{r0:.1f}),趋势健康"


def strategy_gap(ctx, params):
    """缺口识别策略:跳空缺口的类型与交易含义。

    缺口类型:
      - 突破缺口:跳空 >= gap_pct(默认 1%)+ 量比 >= vol_ratio(默认 1.5)+ 突破近 N 日高/低点
      - 中继缺口:趋势中段跳空,不回补(在 N 日区间外但未突破)
      - 衰竭缺口:跳空后 5 日内回补 + 量比放大 + 趋势末段(累计涨幅大)
      - 普通缺口:跳空 < 1%,后续会回补

    规则:
      - 突破缺口 + 向上 → buy(突破行情启动)
      - 突破缺口 + 向下 → sell(破位下跌)
      - 衰竭缺口 + 高位 → sell(趋势末段)
      - 衰竭缺口 + 低位 → buy(跌势末段)
      - 普通缺口 → hold
    """
    gap_pct = float(params.get("gap_pct", 1.0))
    vol_ratio_min = float(params.get("vol_ratio", 1.5))
    n = int(params.get("n", 20))
    exhaustion_lookback = int(params.get("exhaustion_lookback", 5))
    exhaustion_cum_pct = float(params.get("exhaustion_cum_pct", 20))
    i = ctx["i"]
    df = ctx["df"]
    if i < n + 1:
        return "hold", "数据不足"
    today_open = float(df["open"].iloc[i])
    prev_close = float(df["close"].iloc[i - 1])
    if prev_close <= 0:
        return "hold", "数据异常"
    # 跳空幅度
    gap = (today_open - prev_close) / prev_close * 100
    if abs(gap) < 0.1:
        return "hold", f"无跳空(跳空{gap:+.2f}%)"
    avg_vol = df["volume"].iloc[max(0, i - 20):i].mean()
    vol_ratio = float(df["volume"].iloc[i]) / avg_vol if avg_vol > 0 else 0
    # 近 N 日高/低点
    recent_high = float(df["high"].iloc[i - n:i].max())
    recent_low = float(df["low"].iloc[i - n:i].min())
    # 突破缺口:跳空>=1% + 放量 + 突破区间
    if abs(gap) >= gap_pct and vol_ratio >= vol_ratio_min:
        if gap > 0 and today_open > recent_high:
            return "buy", f"向上突破缺口:跳空+{gap:.2f}%+量比{vol_ratio:.1f}+突破{n}日高点{recent_high:.2f},启动行情"
        if gap < 0 and today_open < recent_low:
            return "sell", f"向下突破缺口:跳空{gap:.2f}%+量比{vol_ratio:.1f}+跌破{n}日低点{recent_low:.2f},破位下跌"
    # 衰竭缺口:跳空 + 5 日内回补 + 累计涨幅/跌幅大
    if abs(gap) >= gap_pct * 0.5:
        # 5 日内是否回补(收盘价回到 prev_close 之下/上)
        if i >= exhaustion_lookback:
            # 累计涨幅(近 20 日)
            cum = (df["close"].iloc[i] - df["close"].iloc[max(0, i - 20)]) / df["close"].iloc[max(0, i - 20)] * 100
            for j in range(i + 1, min(i + 1 + exhaustion_lookback, len(df))):
                if gap > 0 and df["close"].iloc[j] < prev_close:
                    # 向上跳空被回补 + 高位 → sell
                    if cum >= exhaustion_cum_pct:
                        return "sell", f"向上衰竭缺口:跳空+{gap:.2f}%+5日内回补+累计涨{cum:.1f}%,趋势末段"
                    break
                if gap < 0 and df["close"].iloc[j] > prev_close:
                    if cum <= -exhaustion_cum_pct:
                        return "buy", f"向下衰竭缺口:跳空{gap:.2f}%+5日内回补+累计跌{cum:.1f}%,跌势末段"
                    break
    # 中继缺口:跳空 + 不回补 + 不突破(在区间内)
    if abs(gap) >= gap_pct * 0.5:
        return "hold", f"中继缺口:跳空{gap:+.2f}%+量比{vol_ratio:.1f},未突破区间,趋势中段"
    # 普通缺口
    return "hold", f"普通缺口:跳空{gap:+.2f}%+量比{vol_ratio:.1f},后续或回补"


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
    rsi6, rsi12 = compute_rsi(df)
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
        "rsi6": round(rsi6.iloc[i], 1) if pd.notna(rsi6.iloc[i]) else 50,
        "rsi12": round(rsi12.iloc[i], 1) if pd.notna(rsi12.iloc[i]) else 50,
    }

    ctx = {
        "i": i,
        "price": price,
        "df": df,
        "close": close,
        "code": code,
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
        "rsi6": rsi6,
        "rsi12": rsi12,
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
        ("rsi", "RSI相对强弱", strategy_rsi),
        ("bottom", "抄底策略", strategy_bottom),
        ("top", "逃顶策略", strategy_top),
        ("zt", "涨停板策略", strategy_zt),
        # 操练大全12章 投资法则
        ("trend_follow", "顺势而为", strategy_trend_follow),
        ("pyramid", "金字塔买卖", strategy_pyramid),
        ("stop_profit", "暴利收手", strategy_stop_profit),
        ("plan_trade", "计划交易", strategy_plan_trade),
        # 漫画书 量能/实战战法
        ("high_volume", "高量柱", strategy_high_volume),
        ("demon_stock", "看妖股", strategy_demon_stock),
        ("dragon_pullback", "龙回头", strategy_dragon_pullback),
        ("support_resistance", "压力支撑", strategy_support_resistance),
        ("range_trade", "区间交易", strategy_range_trade),
        # 操练大全15章 抄底
        ("bottom_ma", "均线识底", strategy_bottom_ma),
        # 操练大全16章 逃顶(周/月线)
        ("top_weekly", "周线见顶", strategy_top_weekly),
        ("top_monthly", "月线见顶", strategy_top_monthly),
        # 操练大全17章 跟庄
        ("zhuang_test", "庄家试盘", strategy_zhuang_test),
        ("zhuang_build", "庄家建仓", strategy_zhuang_build),
        ("zhuang_pull", "庄家拉高", strategy_zhuang_pull),
        ("zhuang_ship", "庄家出货", strategy_zhuang_ship),
        ("zhuang_wash", "庄家洗盘", strategy_zhuang_wash),
        # 操练大全20章 涨停细分
        ("zt_type", "涨停类型", strategy_zt_type),
        ("zt_unsealed", "涨停封不牢", strategy_zt_unsealed),
        ("zt_pull", "拉高型涨停", strategy_zt_pull),
        # 操练大全14章 基本面
        ("pe_select", "市盈率选股", strategy_pe_select),
        ("roe_pe", "ROE+PE选股", strategy_roe_pe),
        # 漫画书 实战战法(剩余)
        ("daban", "打板策略", strategy_daban),
        ("fupan", "复盘法", strategy_fupan),
        # 操练大全15章 抄底(剩余)
        ("bottom_time", "时间识底", strategy_bottom_time),
        # 操练大全14章 选股(剩余)
        ("shareholder_select", "股东人数选股", strategy_shareholder_select),
        ("policy_select", "政策选股", strategy_policy_select),
        # 经典 K 线形态 + 顶背离 + 缺口
        ("kline_pattern", "K线形态", strategy_kline_pattern),
        ("macd_top_divergence", "MACD顶背离", strategy_macd_top_divergence),
        ("rsi_top_divergence", "RSI顶背离", strategy_rsi_top_divergence),
        ("gap", "缺口识别", strategy_gap),
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


# 策略 → 依赖的 ctx 指标字段(只列从 ctx 取的,策略函数内部自调 compute_xxx 的不算)
# 用于 scan_with_strategy 按需计算,避免每股算全套 10+ 指标只用 1 个的浪费
_STRATEGY_DEPS: dict[str, tuple[str, ...]] = {
    "bottom_ma": ("ma10", "ma20", "ma5", "ma60"),
    "bottom_time": ("ma60",),
    "dragon_pullback": ("ma10",),
    "macd_top_divergence": ("macd_diff",),
    "plan_trade": ("ma10", "macd_dea", "macd_diff"),
    "resonance": ("boll_m", "ma5"),
    "rsi_top_divergence": ("rsi6",),
    "trend_follow": ("adx", "ma10", "ma20", "ma5"),
    "tower": ("tower",),
    "zhuang_wash": ("ma20",),
    # 其余 44 策略只用 df/close/i,无额外依赖
}


def _compute_indicator(df, ind: str):
    """按指标名按需计算单个指标,返回 (value or None)。
    用于 scan_with_strategy 按 _STRATEGY_DEPS 只算需要的。
    """
    close = df["close"]
    if ind in ("ma5", "ma10", "ma20", "ma60", "ma7", "ma13"):
        n = int(ind[2:])
        return close.rolling(n).mean()
    if ind == "macd_diff":
        d, _, _ = compute_macd(df)
        return d
    if ind == "macd_dea":
        _, e, _ = compute_macd(df)
        return e
    if ind == "boll_m":
        _, m, _ = compute_boll(df)
        return m
    if ind == "rsi6":
        r6, _ = compute_rsi(df)
        return r6
    if ind == "adx":
        _, _, a = compute_dmi(df)
        return a
    if ind == "tower":
        return compute_tower(df)
    return None


def scan_with_strategy(
    strategy_id: str,
    top_n: int = 20,
    min_amount_yi: float = 0.5,
    limit: int = 0,
    progress_callback=None,
) -> dict:
    """全市场扫描指定策略,返回触发 buy 信号的股票列表。

    轻量版:跳过 fetch_realtime,只跑指定单策略(非 analyze 的全部 45 个),
    适合"哪些股票今天触发了 X 策略买入信号"的选股场景。

    Args:
        strategy_id: 策略 id(必须是 BUILTIN 列表中的内置策略)
        top_n: 返回前 N 只(按涨幅降序),默认 20
        min_amount_yi: 最小成交额(亿)过滤,默认 0.5 亿,过滤小盘股流动性差
        limit: 限制扫描股票数(调试用),0=全市场
        progress_callback: 可选回调 fn(scanned, total, hits_count),用于发进度提示

    Returns: {"strategy": sid, "scanned": n, "hits": [...], "elapsed_sec": float}
             hits: [{code, name, price, pct, signal, reason, amount_yi}, ...]

    实现要点:
      - 只用 daily 表已缓存的股票(不主动 fetch,避免联网慢)
      - 跳过 ST/退市股
      - 单线程跑(策略函数非线程安全,参考 backtest_builtin workers=1)
      - 数据不足(< 60 日)跳过
    """
    import sqlite3
    import time as _time

    t0 = _time.time()

    # 1. 验证策略 id 在 BUILTIN 列表
    builtin_ids = {
        "macd", "kdj", "ma_stop", "boll", "dmi", "psy", "bias", "sar",
        "bbiboll", "tower", "ma_combo", "two_line", "life_line", "three_third",
        "sparrow", "bounce", "volume_div", "resonance", "dmi_psy", "rsi",
        "bottom", "top", "zt",
        "trend_follow", "pyramid", "stop_profit", "plan_trade",
        "high_volume", "demon_stock", "dragon_pullback",
        "support_resistance", "range_trade",
        "bottom_ma", "top_weekly", "top_monthly",
        "zhuang_test", "zhuang_build", "zhuang_pull", "zhuang_ship", "zhuang_wash",
        "zt_type", "zt_unsealed", "zt_pull",
        "pe_select", "roe_pe",
        "daban", "fupan", "bottom_time",
        "shareholder_select", "policy_select",
        "kline_pattern", "macd_top_divergence", "rsi_top_divergence", "gap",
    }
    if strategy_id not in builtin_ids:
        return {"error": f"未知策略 id: {strategy_id},必须是内置策略之一"}

    # 需要联网的策略不适合全市场扫描(每只都要联网,4700 只会跑数小时)
    NO_SCAN_STRATEGIES = {"shareholder_select", "policy_select"}
    if strategy_id in NO_SCAN_STRATEGIES:
        return {
            "error": f"策略 {strategy_id} 需要联网获取外部数据(股东人数/新闻),"
            "不适合全市场扫描,请用 analyze_with_strategy 分析个股"
        }

    # 2. 从 daily 表取所有股票的最新数据(不主动 fetch,避免 4700 次联网)
    # 优化: 不用全表 GROUP BY(450 万行 ~25s),改为只取最新日期的 code 列表(~5000 行 <0.1s)
    # 数据长度过滤交给策略函数自己处理(< 60 天的 macd/kdj 会返回 hold)
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        latest_date = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if not latest_date:
            conn.close()
            return {"error": "daily 表为空,请先运行 daily_scan 抓取数据"}
        # 取最新日期前 7 天内有数据的 code(放宽到 7 天避免停牌股被漏掉)
        # daily 表 date 是 YYYYMMDD 字符串,手动算 7 天前(测试 mock 可能返回非 str,兼容)
        if isinstance(latest_date, str):
            cutoff = (_dt.strptime(latest_date, "%Y%m%d") - _td(days=7)).strftime("%Y%m%d")
        else:
            cutoff = latest_date  # mock 场景,直接用
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    # 过滤:无(数据长度过滤交给策略函数,避免 N+1 查 COUNT)
    # 兼容旧版 (code, n, last) 三元组(测试 mock)和新版 (code,) 单元组
    candidates = [row[0] for row in rows]
    if limit and limit < len(candidates):
        candidates = candidates[:limit]

    # 3. 单线程跑指定策略(策略函数非线程安全)
    fn_name = f"strategy_{strategy_id}"
    fn = globals().get(fn_name)
    if fn is None:
        return {"error": f"策略函数 {fn_name} 不存在"}

    params = {**DEFAULT_STRATEGY_PARAMS.get(strategy_id, {})}
    hits = []
    scanned = 0
    total = len(candidates)
    last_progress_at = 0  # 上次发进度的 scanned 值
    for code in candidates:
        scanned += 1
        # 让出 GIL:给 ws 心跳线程喘息窗口,防止长扫描期间 ping timeout 断连
        if scanned % 50 == 0:
            _time.sleep(0.001)
        # 进度回调:每 200 只或完成时发一次
        if progress_callback and (scanned - last_progress_at >= 200 or scanned == total):
            try:
                progress_callback(scanned, total, len(hits))
            except Exception:
                pass
            last_progress_at = scanned
        try:
            df = get_daily_data(code, days=320)
            if len(df) < 60:
                continue
            close = df["close"]
            i = len(df) - 1
            price = float(close.iloc[i])

            # 跳过 ST/退市(用股票名,但这里没有 name 字段,跳过)

            # 构造 ctx:只算当前策略依赖的指标(其余策略函数内部自调 compute_xxx)
            # 优化:不再预算全套 10+ 指标,4700 股 × 10+ 指标的浪费消除
            ctx = {
                "i": i, "price": price, "df": df, "close": close, "code": code,
                "realtime": None,
            }
            deps = _STRATEGY_DEPS.get(strategy_id, ())
            for ind in deps:
                ctx[ind] = _compute_indicator(df, ind)

            sg, reason = fn(ctx, params)
            if sg != "buy":
                continue

            # 算成交额 + 涨幅(用最新一日)
            last_row = df.iloc[i]
            prev_close = float(close.iloc[i - 1]) if i >= 1 else price
            pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            # 成交额 = 均价 × 成交量(粗估,无成交额字段时用成交量×收盘)
            amount_yi = float(last_row["volume"] * price) / 1e8 if "volume" in df.columns else 0
            if amount_yi < min_amount_yi:
                continue

            hits.append({
                "code": code,
                "name": "",  # 无 name 字段,留空,Bot 端可补
                "price": round(price, 2),
                "pct": round(pct, 2),
                "signal": sg,
                "reason": reason,
                "amount_yi": round(amount_yi, 2),
            })
        except Exception:
            continue

    # 4. 按涨幅降序取 top_n
    hits.sort(key=lambda x: x["pct"], reverse=True)
    hits = hits[:top_n]

    return {
        "strategy": strategy_id,
        "scanned": scanned,
        "hits_count": len(hits),
        "hits": hits,
        "elapsed_sec": round(_time.time() - t0, 1),
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
