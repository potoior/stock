"""玉姐精选：每日全市场扫描打分

每天早上 09:00（服务进程内调度）按玉姐的条件对全 A 股打分，
返回最匹配的 Top 10 股票。所有规则参数可配置（config.json -> yujie）。

打分规则（对应玉姐同花顺条件，默认满分 11）：
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
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

from daily_scan import fetch_market_all, norm_code
from strategy_engine import (
    CONFIG_PATH,
    get_daily_data,
    compute_macd,
    compute_mos_lows,
    compute_rsi,
)

HOME = Path(__file__).parent
CACHE_DB = HOME / "stock_cache.db"

# 默认参数（可被 config.json -> yujie 覆盖）
DEFAULT_PARAMS = {
    "scope": {
        "min_history_days": 60,
        "min_amount_yi": 0.5,  # 成交额下限(亿)，过滤无量垃圾股
        "exclude_sz_code": ["688", "300", "301", "8"],  # 科创板/创业板/北交默认剔除
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
        "ratio": 0.4,  # 现价 ≤ (高+低)×ratio
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


def score_stock(code, params):
    """对单只股票打分，返回 (score, hits, detail) 或 (0, [], None) 数据不足。"""
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

    # 5. MOS 绿色柱子变短（死叉段内）
    if mos["cl1"] is not None and bar_v < 0 and bar_v > bar_prev:
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
    m1, m2, m3, m4 = (int(params["bull_ma"][k]) for k in ("m1", "m2", "m3", "m4"))
    ma_s = float(close.rolling(m1).mean().iloc[i])
    ma_m = float(close.rolling(m2).mean().iloc[i])
    ma_l = float(close.rolling(m3).mean().iloc[i])
    ma_l4 = float(close.rolling(m4).mean().iloc[i])
    if price > ma_s > ma_m > ma_l > ma_l4:
        score += float(params["bull_ma"]["score"])
        hits.append(_hit_label("bull_ma"))
        detail["bull_ma"] = True

    # 9. 120日低位区
    lp = int(params["low_pos"]["period"])
    if i >= lp:
        seg_hi = float(high.iloc[i - lp : i].max())
        seg_lo = float(low.iloc[i - lp : i].min())
        if price <= (seg_hi + seg_lo) * float(params["low_pos"]["ratio"]):
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
        "SELECT rank,code,name,score,hits FROM yujie_picks WHERE date=? ORDER BY rank", (date_str,)
    ).fetchall()
    conn.close()
    return [
        {
            "rank": r[0],
            "code": r[1],
            "name": r[2],
            "score": r[3],
            "hits": json.loads(r[4]) if r[4] else [],
        }
        for r in rows
    ]


# ---------------- 全市场扫描 ----------------


def run_once(limit=0):
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

        with ThreadPoolExecutor(max_workers=16) as ex:
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
    top = results[:50]
    print(f"\n扫描完成，命中 {len(results)} 只，Top 50:")
    for p in top:
        print(f"  {p['score']:>5}  {p['code']} {p['name']}  {p['price']:>8}  {','.join(p['hits'])}")

    save_picks(date_str, top)
    return len(top)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅扫描前N只(调试)")
    args = ap.parse_args()
    t0 = time.time()
    n = run_once(limit=args.limit)
    print(f"耗时 {time.time() - t0:.0f}s，入库 {n} 条")