"""玉姐精选：每日全市场扫描打分

每天早上 09:35（服务进程内调度,开盘后 5 分钟数据稳定）按玉姐的条件对全 A 股打分，
返回全部命中股票（按分数降序）。所有规则参数可配置（config.json -> yujie）。

打分规则（对应玉姐同花顺条件，默认满分 12）:
  1. MACD 金叉            +2   DIFF 上穿 DEA
  2. MACD 即将金叉        +1   DIFF<DEA 且差值极小并拐头向上
  3. MACD 绿色柱子变短    +1   MACD 柱<0 且今日>昨日
  4. MOS 低点(底背离)     +1   CL1<CL2 且 DIFL1>=DIFL2
  5. MOS 绿色柱子变短     +1   死叉段内 MACD 柱<0 且收窄
  6. 突破信号(金叉+突破)  +2   价格突破近N日高点 且 MACD 金叉
  7. RSI 金叉             +1   RSI6 上穿 RSI12
  8. 日线多线多头         +1   MA5>MA10>MA20>MA60
  9. 120日低位区          +1   现价 ≤ (高+低)×低位比例
  10. 距高点回撤≥阈值     +1   距120日高点回撤 ≥ 阈值

用法：
  python yujie_scan.py            # 立即跑一次
  python yujie_scan.py --limit 500  # 调试：仅扫前500只
"""

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pandas as pd

from daily_scan import fetch_market_all, norm_code
from strategy_engine import (
    CONFIG_PATH,
    compute_macd,
    compute_mos_lows,
    compute_rsi,
    get_daily_data,
)

log = logging.getLogger("quant.yujie")

HOME = Path(__file__).parent
CACHE_DB = HOME / "stock_cache.db"

# 默认参数（可被 config.json -> yujie 覆盖）
DEFAULT_PARAMS = {
    "scope": {
        "min_history_days": 60,
        "min_amount_yi": 0.5,  # 成交额下限(亿),过滤流动性差的小盘股
        "exclude_sz_code": [],  # 不剔除任何板块，全 A 股扫描
    },
    "macd": {
        "golden_score": 2,
        "near_size": 0.2,  # 即将金叉：DIFF-DEA 差值阈值
        "near_score": 1,
        "green_shrink_score": 1,
    },
    "mos": {
        "bottom_score": 1,
        "green_shrink_score": 1,
    },
    "breakout": {
        "score": 2,
        "period": 20,  # 突破近 N 日高点
    },
    "rsi": {
        "score": 1,
        "p1": 6,
        "p2": 12,
    },
    "bull_ma": {
        "score": 1,
        "m1": 5,
        "m2": 10,
        "m3": 20,
        "m4": 60,
    },
    "low_pos": {
        "score": 1,
        "period": 120,
        "ratio": 0.4,  # 现价 ≤ 最低点 + (高-低)×ratio,即价格在 120 日区间下半 40% 内
    },
    "drawdown": {
        "score": 1,
        "period": 120,
        "threshold": 0.2,  # 距高点回撤 ≥ 20%
    },
}


def get_params():
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    saved = cfg.get("yujie", {}) or {}

    def _merge(defaults, saved_sub):
        out = dict(defaults)
        if isinstance(saved_sub, dict):
            for k, v in saved_sub.items():
                if isinstance(v, dict) and isinstance(out.get(k), dict):
                    out[k] = _merge(out[k], v)
                else:
                    out[k] = v
        return out

    return _merge(DEFAULT_PARAMS, saved)


def save_params(new_params):
    """合并保存参数到 config.json -> yujie"""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["yujie"] = new_params
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 打分逻辑 ----------------


def _hit_label(rule_id):
    return {
        "macd_golden": "MACD金叉",
        "macd_near": "MACD即将金叉",
        "macd_green": "MACD绿柱缩短",
        "mos_bottom": "MOS低点",
        "mos_green": "MOS绿柱缩短",
        "breakout": "突破+金叉",
        "rsi_golden": "RSI金叉",
        "bull_ma": "多线多头",
        "low_pos": "低位区",
        "drawdown": "深回撤",
    }.get(rule_id, rule_id)


def score_stock(code: str, params: dict, df=None) -> tuple[float, list, dict | None]:
    """对单只股票打分，返回 (score, hits, detail) 或 (0, [], None) 数据不足。

    Args:
        code: 6 位股票代码
        params: 玉姐参数 dict
        df: 可选,外部传入的 K 线 DataFrame(跳过 get_daily_data 联网,加速全市场扫描)
    """
    if df is None:
        df = get_daily_data(code)
    if len(df) < params["scope"]["min_history_days"]:
        return 0, [], None
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    price = float(close.iloc[-1])
    i = len(df) - 1

    diff, dea, bar = compute_macd(df)
    d, e = float(diff.iloc[i]), float(dea.iloc[i])
    bar_v = float(bar.iloc[i])
    bar_prev = float(bar.iloc[i - 1])

    rsi1, rsi2 = compute_rsi(df, int(params["rsi"]["p1"]), int(params["rsi"]["p2"]))
    r1, r2 = float(rsi1.iloc[i]), float(rsi2.iloc[i])

    mos = compute_mos_lows(df, diff=diff, dea=dea)

    score = 0.0
    hits = []
    detail = {}

    # 预算所有需要的 MA 窗口（避免重复 rolling，热路径 5000 次扫描）
    m1, m2, m3, m4 = (int(params["bull_ma"][k]) for k in ("m1", "m2", "m3", "m4"))
    ma_windows = {5, 10, 20, 60, m1, m2, m3, m4}
    ma_cache = {w: float(close.rolling(w).mean().iloc[i]) for w in ma_windows if len(df) >= w}
    ma5_v = ma_cache.get(5)
    ma10_v = ma_cache.get(10)
    ma20_v = ma_cache.get(20)
    ma60_v = ma_cache.get(60)

    detail.update({
        "price": round(price, 2),
        "ma5": round(ma5_v, 2) if ma5_v is not None else None,
        "ma10": round(ma10_v, 2) if ma10_v is not None else None,
        "ma20": round(ma20_v, 2) if ma20_v is not None else None,
        "ma60": round(ma60_v, 2) if ma60_v is not None else None,
        "macd_dif": round(d, 3), "macd_dea": round(e, 3),
        "macd_bar": round(bar_v, 3),
        "rsi6": round(r1, 1), "rsi12": round(r2, 1),
        "cl1": mos["cl1"], "cl2": mos["cl2"],
        "difl1": mos["difl1"], "difl2": mos["difl2"],
    })

    # 1. MACD 金叉 + 6. 突破信号（金叉判定）
    golden = bool(diff.iloc[i] > dea.iloc[i] and diff.iloc[i - 1] <= dea.iloc[i - 1])
    if golden:
        score += float(params["macd"]["golden_score"])
        hits.append(_hit_label("macd_golden"))
        detail["macd_golden"] = True

    # 2. MACD 即将金叉
    if not golden and d < e and (e - d) < float(params["macd"]["near_size"]):
        # 拐头向上：DIFF 今日较昨日抬升
        if diff.iloc[i] > diff.iloc[i - 1]:
            score += float(params["macd"]["near_score"])
            hits.append(_hit_label("macd_near"))
            detail["macd_near"] = True

    # 3. MACD 绿色柱子变短
    if bar_v < 0 and bar_v > bar_prev:
        score += float(params["macd"]["green_shrink_score"])
        hits.append(_hit_label("macd_green"))
        detail["macd_green"] = True

    # 4. MOS 低点（底背离）
    if mos["bottom"]:
        score += float(params["mos"]["bottom_score"])
        hits.append(_hit_label("mos_bottom"))
        detail["mos_bottom"] = True

    # 5. MOS 绿色柱子变短(死叉段内: DIFF<DEA 时柱子缩短为反转信号)
    #    修正: 旧版只比 bar_v < 0 > bar_prev,与第3条 macd_green 完全等价导致重复加分
    #    新版加死叉段判定 diff < dea,与 macd_green 区分开
    if mos["cl1"] is not None and bar_v < 0 and bar_v > bar_prev and d < e:
        score += float(params["mos"]["green_shrink_score"])
        hits.append(_hit_label("mos_green"))
        detail["mos_green"] = True

    # 6. 突破信号：突破近 N 日高点 + MACD 金叉
    period = int(params["breakout"]["period"])
    if i >= period:
        prev_high = float(high.iloc[i - period : i].max())
        if golden and price > prev_high:
            score += float(params["breakout"]["score"])
            hits.append(_hit_label("breakout"))
            detail["breakout"] = True

    # 7. RSI 金叉
    if r1 > r2 and float(rsi1.iloc[i - 1]) <= float(rsi2.iloc[i - 1]):
        score += float(params["rsi"]["score"])
        hits.append(_hit_label("rsi_golden"))
        detail["rsi_golden"] = True

    # 8. 多线多头
    ma_s = ma_cache.get(m1)
    ma_m = ma_cache.get(m2)
    ma_l = ma_cache.get(m3)
    ma_l4 = ma_cache.get(m4)
    if all(v is not None for v in (ma_s, ma_m, ma_l, ma_l4)) and price > ma_s > ma_m > ma_l > ma_l4:
        score += float(params["bull_ma"]["score"])
        hits.append(_hit_label("bull_ma"))
        detail["bull_ma"] = True

    # 9. 120日低位区: 价格在区间下方 ratio 比例内
    #    修正: 旧公式 price <= (hi+lo)*ratio 随高低差漂移,语义错误
    #    新公式: price <= lo + (hi-lo)*ratio (ratio=0.4 即价格在最低点上方 40% 区间内)
    lp = int(params["low_pos"]["period"])
    if i >= lp:
        seg_hi = float(high.iloc[i - lp : i].max())
        seg_lo = float(low.iloc[i - lp : i].min())
        if seg_hi > seg_lo and price <= seg_lo + (seg_hi - seg_lo) * float(params["low_pos"]["ratio"]):
            score += float(params["low_pos"]["score"])
            hits.append(_hit_label("low_pos"))
            detail["low_pos"] = True

    # 10. 距高点回撤
    dd = int(params["drawdown"]["period"])
    if i >= dd:
        dd_hi = float(high.iloc[i - dd : i].max())
        if dd_hi > 0 and (dd_hi - price) / dd_hi >= float(params["drawdown"]["threshold"]):
            score += float(params["drawdown"]["score"])
            hits.append(_hit_label("drawdown"))
            detail["drawdown"] = True

    return round(score, 1), hits, detail


# ---------------- 持久化 ----------------


def _db():
    conn = sqlite3.connect(str(CACHE_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS yujie_picks (
            date TEXT, rank INTEGER, code TEXT, name TEXT,
            score REAL, hits TEXT, detail TEXT,
            PRIMARY KEY (date, code)
        )""")
    conn.commit()
    return conn


def save_picks(date_str, picks):
    conn = _db()
    conn.execute("DELETE FROM yujie_picks WHERE date=?", (date_str,))
    for rank, p in enumerate(picks, 1):
        conn.execute(
            "INSERT OR REPLACE INTO yujie_picks(date,rank,code,name,score,hits,detail) VALUES(?,?,?,?,?,?,?)",
            (date_str, rank, p["code"], p["name"], p["score"], json.dumps(p["hits"], ensure_ascii=False),
             json.dumps(p["detail"], ensure_ascii=False)),
        )
    conn.commit()
    conn.close()


def load_picks(date_str=None):
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    conn = _db()
    rows = conn.execute(
        "SELECT rank,code,name,score,hits,detail FROM yujie_picks WHERE date=? ORDER BY rank", (date_str,)
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        item = {
            "rank": r[0],
            "code": r[1],
            "name": r[2],
            "score": r[3],
            "hits": json.loads(r[4]) if r[4] else [],
        }
        if r[5]:
            item["detail"] = json.loads(r[5])
        out.append(item)
    return out


def get_rank(code, date_str=None):
    """直接 SQL 查单股当日排名，避免 load_picks() 全表加载。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    conn = _db()
    row = conn.execute(
        "SELECT rank FROM yujie_picks WHERE date=? AND code=?", (date_str, code)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------- 全市场扫描 ----------------


def run_once(limit: int = 0) -> int:
    params = get_params()
    date_str = datetime.now().strftime("%Y%m%d")
    print(f"== 玉姐精选扫描 {date_str} ==")

    rows = fetch_market_all(limit=limit)
    if not rows:
        print("行情抓取失败")
        return 0
    print(f"全市场 {len(rows)} 只")
    log_fp = HOME / "yujie_scan.log"
    with open(log_fp, "a", encoding="utf-8") as lf:
        lf.write(
            f"== {datetime.now():%Y-%m-%d %H:%M:%S} 玉姐精选扫描 {date_str} "
            f"全市场实时清单 {len(rows)} 只 ==\n"
        )

    excl = params["scope"].get("exclude_sz_code", [])
    min_amt = float(params["scope"].get("min_amount_yi", 0.5))
    pool = []
    for r in rows:
        code = norm_code(r.get("symbol", ""))[-6:].zfill(6)
        name = r.get("name", "")
        if "ST" in name or "退" in name:
            continue
        if any(code.startswith(p) for p in excl):
            continue
        amt_yi = float(r.get("amount") or 0) / 1e8
        if amt_yi < min_amt:
            continue
        pool.append((code, name, float(r.get("trade") or 0), float(r.get("changepercent") or 0)))
    print(f"过滤后候选池 {len(pool)} 只")

    results = []
    scanned = 0
    lock = threading.Lock()
    with open(log_fp, "a", encoding="utf-8") as lf:
        lf.write(f"候选池 {len(pool)} 只\n")
        msg0 = f"  已扫描 0/{len(pool)} 开始并发扫描...\n"
        lf.write(msg0)

        def _worker(item):
            code, name, price, pct = item
            try:
                sc, hits, detail = score_stock(code, params)
            except Exception:
                log.exception("score_stock 异常 %s %s", code, name)
                sc, hits, detail = 0, [], None
            return code, name, price, pct, sc, hits, detail or {}

        def _on_done(fut):
            nonlocal scanned
            code, name, price, pct, sc, hits, detail = fut.result()
            with lock:
                scanned += 1
                if sc > 0:
                    results.append({"code": code, "name": name, "price": price, "pct": round(pct, 2), "score": sc, "hits": hits, "detail": detail})
                if scanned % 1000 == 0:
                    msg = f"  已扫描 {scanned}/{len(pool)} (含缓存，命中 {len(results)})"
                    print(msg)
                    lf.write(msg + "\n")
                    lf.flush()

        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = [ex.submit(_worker, item) for item in pool]
            for f in futs:
                f.add_done_callback(_on_done)
            for f in futs:
                try:
                    f.result()
                except Exception:
                    pass
        lf.write(f"扫描完成 命中 {len(results)} 只，耗时见上方\n")

    results.sort(key=lambda x: -x["score"])
    print(f"\n扫描完成，命中 {len(results)} 只，全部入库")
    for p in results[:20]:
        print(f"  {p['score']:>5}  {p['code']} {p['name']}  {p['price']:>8}  {','.join(p['hits'])}")

    save_picks(date_str, results)
    return len(results)


def scan_all_cached(
    top_n: int = 20,
    min_score: float = 5.0,
    limit: int = 0,
    progress_callback=None,
) -> dict:
    """全市场玉姐评分扫描(用 daily 表已缓存数据,不联网)。

    与 run_once 区别:run_once 先从新浪抓全市场实时行情(联网)再评分;
    本函数直接用 daily 表已缓存的 4700+ 只股票评分,不联网,速度快(1-3 分钟)。

    Args:
        top_n: 返回前 N 只(按评分降序),默认 20
        min_score: 最低评分门槛,默认 5.0(玉姐精选默认门槛)
        limit: 限制扫描股票数(调试用),0=全市场
        progress_callback: 可选回调 fn(scanned, total, hits_count)

    Returns: {scanned, hits_count, hits, elapsed_sec}
             hits: [{code, score, hits, price, ...}, ...] 按评分降序
    """
    import time as _time

    t0 = _time.time()
    params = get_params()

    # 1. 从 daily 表取所有有缓存的股票(同 scan_with_strategy 优化思路)
    # 优化: 不用 N+1 sqlite(每只单独 connect + read_sql),改为先拿 candidates 再批量拉
    from strategy_engine import CACHE_DB
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        # 1a. 用 GROUP BY 拿 candidates(冷启动 25s,热缓存 0.3s,可接受)
        rows = conn.execute(
            "SELECT code, MAX(date) as last FROM daily GROUP BY code"
        ).fetchall()
        # 1b. 过滤:最新日期近 7 日内(避免陈旧缓存)
        today = datetime.now().strftime("%Y%m%d")
        cutoff_today = str(int(today) - 7) if today.isdigit() else today
        # 兼容 (code, last) 和 (code, n, last) 两种 row 格式
        candidates = []
        for row in rows:
            code = row[0]
            last = row[-1]
            if last and last >= cutoff_today:
                candidates.append(code)
        if limit and limit < len(candidates):
            candidates = candidates[:limit]

        # 1c. 一次性批量拉取 candidates 的全部历史数据(避免 N+1)
        # 用 IN(...) 限制只拉所需股票,320 天 × N 只
        if not candidates:
            conn.close()
            return {"scanned": 0, "hits_count": 0, "hits": [], "elapsed_sec": 0.0}
        placeholders = ",".join("?" * len(candidates))
        bulk_df = pd.read_sql(
            f"SELECT code, date, open, close, high, low, volume FROM daily WHERE code IN ({placeholders})",
            conn,
            params=candidates,
        )
        bulk_df["date"] = pd.to_datetime(bulk_df["date"], format="%Y%m%d", errors="coerce")
    finally:
        conn.close()

    # 按 code 分组(每个 code 一个 df,供 worker 直接取)
    grouped = {code: g.sort_values("date").reset_index(drop=True)
               for code, g in bulk_df.groupby("code", sort=False)}

    total = len(candidates)

    results = []
    scanned = [0]
    lock = threading.Lock()

    def _worker(code):
        try:
            df = grouped.get(code)
            if df is None or len(df) < 60:
                return "", 0, [], None
            sc, hits, detail = score_stock(code, params, df=df)
        except Exception:
            sc, hits, detail = 0, [], None
        return code, sc, hits, detail

    last_progress = [0.0]
    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(_worker, c): c for c in candidates}
        for fut in futs:
            try:
                code, sc, hits, detail = fut.result()
            except Exception:
                code, sc, hits, detail = "", 0, [], None
            with lock:
                scanned[0] += 1
                if sc >= min_score and detail:
                    results.append({
                        "code": code,
                        "score": sc,
                        "hits": hits,
                        "price": detail.get("price"),
                        "ma5": detail.get("ma5"),
                        "ma10": detail.get("ma10"),
                        "ma20": detail.get("ma20"),
                        "macd_dif": detail.get("macd_dif"),
                        "rsi6": detail.get("rsi6"),
                    })
                # 进度回调:每 500 只或完成时
                if progress_callback and (
                    scanned[0] - last_progress[0] >= 500 or scanned[0] == total
                ):
                    try:
                        progress_callback(scanned[0], total, len(results))
                    except Exception:
                        pass
                    last_progress[0] = scanned[0]

    # 3. 按评分降序取 top_n
    results.sort(key=lambda x: -x["score"])
    hits = results[:top_n]

    return {
        "scanned": scanned[0],
        "hits_count": len(hits),
        "hits": hits,
        "elapsed_sec": round(_time.time() - t0, 1),
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅扫描前N只(调试)")
    args = ap.parse_args()
    t0 = time.time()
    n = run_once(limit=args.limit)
    print(f"耗时 {time.time() - t0:.0f}s，入库 {n} 条")
