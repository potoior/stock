"""玉姐精选评分回测：验证打分体系是否具有超额收益。

思路：
  1. prepare_history(): 对全市场股票池拉 1024 日深历史补全缓存（~4 年）
  2. score_series():    按日向量化重算玉姐 10 条规则，得到每日评分序列（无前视）
  3. run_backtest():    信号日（评分>0）收盘买入，持有 N 天统计前向收益；
                       按评分分桶检验区分度；与"同股随机入场"基准对比

用法：
  python backtest_yujie.py prepare              # 仅补全深历史
  python backtest_yujie.py backtest             # 仅跑回测（需先 prepare）
  python backtest_yujie.py all                  # 先 prepare 再 backtest
  python backtest_yujie.py all --limit 200      # 调试：只跑前 200 只
  python backtest_yujie.py backtest --skip-prepare
"""

import argparse
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from strategy_engine import (
    CACHE_DB,
    _ensure_daily_table,
    compute_macd,
    compute_rsi,
    fetch_qfq_tencent,
)
from yujie_scan import get_params

HOME = Path(__file__).parent
REPORT_MD = HOME / "yujie_backtest_report.md"
REPORT_JSON = HOME / "yujie_backtest_report.json"

WARMUP = 120          # 120 日低位/回撤规则需要的前置天数
HORIZONS = (5, 10, 20, 60)
SCORE_BUCKETS = ((1, 2, "1-2"), (3, 4, "3-4"), (5, 6, "5-6"), (7, 99, "7+"))


# ============================================================
# 1. 历史补全
# ============================================================


def _get_universe_codes() -> list[str]:
    """从 daily 表去重取得当前已扫描的全部股票代码（~4300+）。"""
    conn = sqlite3.connect(str(CACHE_DB))
    rows = conn.execute("SELECT DISTINCT code FROM daily ORDER BY code").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _history_len(code: str) -> int:
    conn = sqlite3.connect(str(CACHE_DB))
    n = conn.execute("SELECT COUNT(*) FROM daily WHERE code=?", (code,)).fetchone()[0]
    conn.close()
    return n


def _refetch_one(code: str, datalen: int, write_conn: sqlite3.Connection,
                  write_lock: threading.Lock) -> int:
    """拉取深历史并写入缓存，返回新增/覆盖行数。失败返回 0。"""
    try:
        df = fetch_qfq_tencent(code, datalen=datalen)
    except Exception:
        df = pd.DataFrame()
    if df is None or len(df) == 0:
        return 0
    rows = df[["code", "date", "open", "close", "high", "low", "volume"]].values.tolist()
    with write_lock:
        write_conn.executemany(
            "INSERT OR REPLACE INTO daily(code,date,open,close,high,low,volume) VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        write_conn.commit()
    return len(rows)


def prepare_history(limit: int = 0, workers: int = 32, datalen: int = 1024,
                    min_days_skip: int = 800) -> int:
    """对历史不足 min_days_skip 的股票补全深历史。返回补全股票数。"""
    _ensure_daily_table()
    codes = _get_universe_codes()
    # 只补全历史不足的，已有深历史的跳过
    todo = [c for c in codes if _history_len(c) < min_days_skip]
    if limit:
        todo = todo[:limit]
    print(f"== 历史补全 == 总 {len(codes)} 只，待补全 {len(todo)} 只（datalen={datalen}）")
    write_conn = sqlite3.connect(str(CACHE_DB), timeout=30, check_same_thread=False)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_conn.execute("PRAGMA synchronous=NORMAL")
    write_conn.execute("PRAGMA busy_timeout=30000")
    write_lock = threading.Lock()
    t0 = time.time()
    done = 0
    ok = 0
    lock = threading.Lock()

    def _work(c):
        return c, _refetch_one(c, datalen, write_conn, write_lock)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_work, c) for c in todo]
        for f in futs:
            try:
                c, n = f.result()
            except Exception:
                n = 0
            with lock:
                done += 1
                if n > 0:
                    ok += 1
                if done % 200 == 0 or done == len(todo):
                    print(f"  {done}/{len(todo)}  成功 {ok}  耗时 {time.time()-t0:.0f}s")
    write_conn.close()
    print(f"补全完成：成功 {ok}/{len(todo)}，耗时 {time.time()-t0:.0f}s")
    return ok


# ============================================================
# 2. 向量化按日重打分
# ============================================================


def _vectorized_mos(low: pd.Series, dif: pd.Series, dea: pd.Series):
    """逐日计算 MOS 低点相关量（与 compute_mos_lows 末日逻辑对齐）。

    返回 (cl1, difl1, cl2, difl2, bottom, has_death)，均为长度 n 的 numpy 数组。
    - has_death[i]: 截至第 i 日是否出现过死叉（cl1/difl1 才有意义）
    - cl1[i]/difl1[i]: 最近死叉段内 [death_idx, i] 的最低 low / 最低 DIFF
    - cl2[i]/difl2[i]: 前一死叉段 [prev_death, last_death] 的最低 low / 最低 DIFF
    - bottom[i]: CL1<CL2 且 DIFL1>=DIFL2（底背离）
    """
    n = len(low)
    dif_arr = dif.values
    # 死叉：DEA 上穿 DIFF（dea>diff 且 前一日 dea<=diff）
    death = ((dea > dif) & (dea.shift(1) <= dif.shift(1))).values
    death_idx = np.where(death)[0]

    cl1 = np.full(n, np.nan)
    difl1 = np.full(n, np.nan)
    cl2 = np.full(n, np.nan)
    difl2 = np.full(n, np.nan)
    bottom = np.zeros(n, dtype=bool)
    has_death = np.zeros(n, dtype=bool)

    if len(death_idx) == 0:
        return cl1, difl1, cl2, difl2, bottom, has_death

    # 每日的段 id：最近的死叉索引在 death_idx 中的位置，-1 表示尚未出现死叉
    seg_id = np.searchsorted(death_idx, np.arange(n), side="right") - 1
    has_death = seg_id >= 0

    # cl1/difl1：段内累计最小值（段从 death_idx[k] 开始）
    seg_series = pd.Series(seg_id, index=low.index)
    cl1 = low.groupby(seg_series).cummin().values
    difl1 = dif.groupby(seg_series).cummin().values
    # 段内 cummin 对 seg_id=-1 的段也算了，屏蔽尚未出现死叉的日子
    cl1 = np.where(has_death, cl1, np.nan)
    difl1 = np.where(has_death, difl1, np.nan)

    # cl2/difl2：前一死叉段 [death_idx[k-1], death_idx[k]] 的最小值（含两端）
    # 对段 k>=0，prev 段范围 = [death_idx[k-1], death_idx[k]]（k=0 时为 [0, death_idx[0]]）
    cl2_map = {}
    difl2_map = {}
    for k, last in enumerate(death_idx):
        if k == 0:
            lo = low.values[: last + 1].min()
            dl = dif_arr[: last + 1].min()
        else:
            prev = death_idx[k - 1]
            lo = low.values[prev : last + 1].min()
            dl = dif_arr[prev : last + 1].min()
        cl2_map[k] = lo
        difl2_map[k] = dl
    for i in range(n):
        s = seg_id[i]
        if s >= 0:
            cl2[i] = cl2_map[s]
            difl2[i] = difl2_map[s]

    has_death_mask = has_death
    valid = has_death_mask & ~np.isnan(cl1) & ~np.isnan(cl2)
    bottom = valid & (cl1 < cl2) & (difl1 >= difl2)
    return cl1, difl1, cl2, difl2, bottom, has_death


def score_series(code: str, params: dict) -> pd.DataFrame | None:
    """对单只股票按日重算玉姐 10 条规则，返回含 score/hits 的 DataFrame。

    返回列: date, close, score, macd_golden, macd_near, macd_green,
           mos_bottom, mos_green, breakout, rsi_golden, bull_ma, low_pos, drawdown
    数据不足返回 None。无前视：第 i 日只用 ≤i 的数据。
    """
    conn = sqlite3.connect(str(CACHE_DB), timeout=10)
    df = pd.read_sql(
        "SELECT date, open, close, high, low, volume FROM daily WHERE code=? ORDER BY date",
        conn, params=(code,),
    )
    conn.close()
    if len(df) < params["scope"]["min_history_days"]:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    diff, dea, bar = compute_macd(df)
    rsi1, rsi2 = compute_rsi(df, int(params["rsi"]["p1"]), int(params["rsi"]["p2"]))
    cl1, difl1, cl2, difl2, bottom, has_death = _vectorized_mos(low, diff, dea)

    # --- 1. MACD 金叉 ---
    golden = (diff > dea) & (diff.shift(1) <= dea.shift(1))
    # --- 2. MACD 即将金叉 ---
    near = (~golden) & (diff < dea) & ((dea - diff) < float(params["macd"]["near_size"])) & (
        diff > diff.shift(1)
    )
    # --- 3. MACD 绿柱缩短 ---
    green = (bar < 0) & (bar > bar.shift(1))
    # --- 4. MOS 底背离 ---
    mos_bottom = pd.Series(bottom, index=df.index)
    # --- 5. MOS 绿柱缩短（死叉段内）---
    mos_green = pd.Series(has_death, index=df.index) & (bar < 0) & (bar > bar.shift(1))
    # --- 6. 突破 + 金叉 ---
    period = int(params["breakout"]["period"])
    prev_high = high.rolling(period).max().shift(1)
    breakout = golden & (close > prev_high)
    # --- 7. RSI 金叉 ---
    rsi_golden = (rsi1 > rsi2) & (rsi1.shift(1) <= rsi2.shift(1))
    # --- 8. 多线多头 ---
    m1, m2, m3, m4 = (int(params["bull_ma"][k]) for k in ("m1", "m2", "m3", "m4"))
    ma_s = close.rolling(m1).mean()
    ma_m = close.rolling(m2).mean()
    ma_l = close.rolling(m3).mean()
    ma_l4 = close.rolling(m4).mean()
    bull_ma = (
        (close > ma_s) & (ma_s > ma_m) & (ma_m > ma_l) & (ma_l > ma_l4)
    )
    # --- 9. 120 日低位区 ---
    lp = int(params["low_pos"]["period"])
    seg_hi = high.rolling(lp).max().shift(1)
    seg_lo = low.rolling(lp).min().shift(1)
    low_pos = close <= (seg_hi + seg_lo) * float(params["low_pos"]["ratio"])
    # --- 10. 距高点回撤 ---
    dd = int(params["drawdown"]["period"])
    dd_hi = high.rolling(dd).max().shift(1)
    drawdown = (dd_hi > 0) & ((dd_hi - close) / dd_hi >= float(params["drawdown"]["threshold"]))

    score = (
        golden.astype(float) * float(params["macd"]["golden_score"])
        + near.astype(float) * float(params["macd"]["near_score"])
        + green.astype(float) * float(params["macd"]["green_shrink_score"])
        + mos_bottom.astype(float) * float(params["mos"]["bottom_score"])
        + mos_green.astype(float) * float(params["mos"]["green_shrink_score"])
        + breakout.astype(float) * float(params["breakout"]["score"])
        + rsi_golden.astype(float) * float(params["rsi"]["score"])
        + bull_ma.astype(float) * float(params["bull_ma"]["score"])
        + low_pos.astype(float) * float(params["low_pos"]["score"])
        + drawdown.astype(float) * float(params["drawdown"]["score"])
    )

    out = pd.DataFrame(
        {
            "date": df["date"],
            "close": close.values,
            "score": score.round(2).values,
            "macd_golden": golden.values,
            "macd_near": near.values,
            "macd_green": green.values,
            "mos_bottom": mos_bottom.values,
            "mos_green": mos_green.values,
            "breakout": breakout.values,
            "rsi_golden": rsi_golden.values,
            "bull_ma": bull_ma.values,
            "low_pos": low_pos.values,
            "drawdown": drawdown.values,
        }
    )
    # 预热期不计信号
    out.loc[: WARMUP - 1, "score"] = 0.0
    for col in ("macd_golden", "macd_near", "macd_green", "mos_bottom", "mos_green",
                "breakout", "rsi_golden", "bull_ma", "low_pos", "drawdown"):
        out.loc[: WARMUP - 1, col] = False
    return out


# ============================================================
# 3. 回测
# ============================================================


def _bucket(score: float) -> str | None:
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return None


def run_backtest(limit: int = 0, workers: int = 16) -> dict:
    params = get_params()
    codes = _get_universe_codes()
    if limit:
        codes = codes[:limit]
    print(f"== 玉姐精选回测 == 股票 {len(codes)} 只，持有期 {HORIZONS} 天")
    t0 = time.time()

    signals: list[dict] = []   # 每个信号一条
    baseline_acc = {h: [0.0, 0] for h in HORIZONS}  # h -> [sum_ret, count] 同股随机入场
    done = 0
    lock = threading.Lock()

    def _work(c):
        try:
            s = score_series(c, params)
        except Exception:
            s = None
        return c, s

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_work, c) for c in codes]
        for f in futs:
            try:
                c, s = f.result()
            except Exception:
                c, s = "", None
            with lock:
                done += 1
                if done % 500 == 0 or done == len(codes):
                    print(f"  评分 {done}/{len(codes)}  信号 {len(signals)}  耗时 {time.time()-t0:.0f}s")
            if s is None or len(s) == 0:
                continue
            closes = s["close"].values
            scores = s["score"].values
            n = len(s)
            # 信号日前向收益
            for i in range(n):
                sc = scores[i]
                if sc <= 0:
                    continue
                rec = {"code": c, "date": s["date"].iloc[i], "score": float(sc),
                       "bucket": _bucket(float(sc))}
                for h in HORIZONS:
                    j = i + h
                    if j < n:
                        rec[f"ret_{h}"] = float(closes[j] / closes[i] - 1)
                    else:
                        rec[f"ret_{h}"] = None
                signals.append(rec)
            # 基准：同股全日期（预热后）随机入场平均前向收益
            for h in HORIZONS:
                tot = 0.0
                cnt = 0
                for i in range(WARMUP, n - h):
                    tot += closes[i + h] / closes[i] - 1
                    cnt += 1
                if cnt:
                    baseline_acc[h][0] += tot
                    baseline_acc[h][1] += cnt

    print(f"评分完成：信号 {len(signals)} 条，耗时 {time.time()-t0:.0f}s")

    # ---- 汇总 ----
    report = _aggregate(signals, baseline_acc)
    return report


def _aggregate(signals: list[dict], baseline_acc: dict) -> dict:
    """汇总信号统计与分桶、基准对比。"""
    report: dict = {"horizons": {}, "buckets": {}, "signal_count": len(signals)}

    for h in HORIZONS:
        rets = [s[f"ret_{h}"] for s in signals if s[f"ret_{h}"] is not None]
        if not rets:
            continue
        arr = np.array(rets)
        base_sum, base_cnt = baseline_acc[h]
        base_mean = base_sum / base_cnt if base_cnt else 0.0
        report["horizons"][h] = {
            "n": int(len(arr)),
            "hit_rate": float((arr > 0).mean()),
            "mean_ret": float(arr.mean()),
            "median_ret": float(np.median(arr)),
            "std_ret": float(arr.std()),
            "baseline_mean_ret": float(base_mean),
            "excess": float(arr.mean() - base_mean),
        }

    # 分桶
    for h in HORIZONS:
        by_bucket: dict[str, list[float]] = {}
        for s in signals:
            r = s[f"ret_{h}"]
            b = s["bucket"]
            if r is None or b is None:
                continue
            by_bucket.setdefault(b, []).append(r)
        report["buckets"][h] = {}
        for _lo, _hi, label in SCORE_BUCKETS:
            arr = by_bucket.get(label, [])
            if not arr:
                continue
            a = np.array(arr)
            report["buckets"][h][label] = {
                "n": int(len(a)),
                "hit_rate": float((a > 0).mean()),
                "mean_ret": float(a.mean()),
                "median_ret": float(np.median(a)),
            }
    return report


# ============================================================
# 4. 报告输出
# ============================================================


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def write_report(report: dict) -> None:
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: list[str] = []
    lines.append("# 玉姐精选评分回测报告\n")
    lines.append(f"信号总数：{report['signal_count']}\n")
    lines.append(f"持有期：{HORIZONS} 天（信号日收盘买入，持有 N 天收盘卖出）\n")
    lines.append(f"评分分桶：{', '.join(b[2] for b in SCORE_BUCKETS)}\n\n")

    lines.append("## 一、整体表现 vs 同股随机入场基准\n")
    lines.append("| 持有期 | 信号数 | 命中率 | 平均收益 | 中位收益 | 基准收益 | 超额 |")
    lines.append("|--------|--------|--------|----------|----------|----------|------|")
    for h in HORIZONS:
        r = report["horizons"].get(h)
        if not r:
            continue
        lines.append(
            f"| {h}天 | {r['n']} | {r['hit_rate']*100:.1f}% | "
            f"{_fmt_pct(r['mean_ret'])} | {_fmt_pct(r['median_ret'])} | "
            f"{_fmt_pct(r['baseline_mean_ret'])} | {_fmt_pct(r['excess'])} |"
        )
    lines.append("")

    lines.append("## 二、按评分分桶（检验区分度）\n")
    for h in HORIZONS:
        lines.append(f"### 持有 {h} 天\n")
        lines.append("| 评分档 | 样本数 | 命中率 | 平均收益 | 中位收益 |")
        lines.append("|--------|--------|--------|----------|----------|")
        for _lo, _hi, label in SCORE_BUCKETS:
            r = report["buckets"].get(h, {}).get(label)
            if not r:
                continue
            lines.append(
                f"| {label} | {r['n']} | {r['hit_rate']*100:.1f}% | "
                f"{_fmt_pct(r['mean_ret'])} | {_fmt_pct(r['median_ret'])} |"
            )
        lines.append("")

    lines.append("## 结论要点\n")
    lines.append("- 若「超额」稳定为正，说明玉姐精选相对随机选时有 alpha；")
    lines.append("- 若高评分档平均收益单调高于低分档，说明评分有区分度；")
    lines.append("- 命中率 > 50% 且平均收益为正，说明信号方向有效。\n")

    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入：{REPORT_MD}")
    print(f"报告已写入：{REPORT_JSON}")


def print_summary(report: dict) -> None:
    print("\n" + "=" * 76)
    print("玉姐精选回测汇总")
    print(f"信号总数：{report['signal_count']}")
    print("=" * 76)
    print(f"{'持有期':>6} {'信号数':>6} {'命中率':>7} {'平均收益':>9} {'基准收益':>9} {'超额':>9}")
    for h in HORIZONS:
        r = report["horizons"].get(h)
        if not r:
            continue
        print(f"{h:>4}天 {r['n']:>6} {r['hit_rate']*100:>6.1f}% "
              f"{_fmt_pct(r['mean_ret']):>9} {_fmt_pct(r['baseline_mean_ret']):>9} "
              f"{_fmt_pct(r['excess']):>9}")
    print("-" * 76)
    print("按评分分桶（平均收益 / 命中率）:")
    print(f"{'档位':>6} | " + " | ".join(f"{h}天".center(18) for h in HORIZONS))
    for _lo, _hi, label in SCORE_BUCKETS:
        cells = []
        for h in HORIZONS:
            r = report["buckets"].get(h, {}).get(label)
            if r:
                cells.append(f"{_fmt_pct(r['mean_ret'])} ({r['hit_rate']*100:.0f}%)")
            else:
                cells.append("-")
        print(f"{label:>6} | " + " | ".join(c.center(18) for c in cells))
    print("=" * 76)


# ============================================================
# 5. 参数网格扫描寻优
# ============================================================

# 网格默认取值：对 4 个阈值参数搜索（其余周期固定 120，权重保持默认）
GRID_DEFAULT = {
    "macd.near_size": [0.10, 0.15, 0.20, 0.30],
    "breakout.period": [10, 20, 40, 60],
    "drawdown.threshold": [0.15, 0.20, 0.30],
    "low_pos.ratio": [0.30, 0.40, 0.50],
}
GRID_REPORT_MD = HOME / "yujie_grid_report.md"
GRID_REPORT_JSON = HOME / "yujie_grid_report.json"


def _rich_flags(code: str, params: dict, grid: dict, horizon: int) -> pd.DataFrame | None:
    """一次性算出单只股票所有候选阈值对应的命中标志列 + 前向收益。

    返回列: date, close, ret_H, warmup, 以及各 flag 列(golden/green/mos_bottom/
    mos_green/rsi_golden/bull_ma 固定；near_*/breakout_*/drawdown_*/lowpos_* 可变)。
    """
    conn = sqlite3.connect(str(CACHE_DB), timeout=10)
    df = pd.read_sql(
        "SELECT date, close, high, low FROM daily WHERE code=? ORDER BY date",
        conn, params=(code,),
    )
    conn.close()
    if len(df) < params["scope"]["min_history_days"]:
        return None
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    n = len(df)

    diff, dea, bar = compute_macd(df)
    rsi1, rsi2 = compute_rsi(df, int(params["rsi"]["p1"]), int(params["rsi"]["p2"]))
    _cl1, _difl1, _cl2, _difl2, bottom, has_death = _vectorized_mos(low, diff, dea)

    # 固定规则
    golden = (diff > dea) & (diff.shift(1) <= dea.shift(1))
    green = (bar < 0) & (bar > bar.shift(1))
    mos_bottom = pd.Series(bottom, index=df.index)
    mos_green = pd.Series(has_death, index=df.index) & (bar < 0) & (bar > bar.shift(1))
    rsi_golden = (rsi1 > rsi2) & (rsi1.shift(1) <= rsi2.shift(1))
    m1, m2, m3, m4 = (int(params["bull_ma"][k]) for k in ("m1", "m2", "m3", "m4"))
    ma_s = close.rolling(m1).mean()
    ma_m = close.rolling(m2).mean()
    ma_l = close.rolling(m3).mean()
    ma_l4 = close.rolling(m4).mean()
    bull_ma = (close > ma_s) & (ma_s > ma_m) & (ma_m > ma_l) & (ma_l > ma_l4)

    out = pd.DataFrame({"close": close.values})
    out["golden"] = golden.values
    out["green"] = green.values
    out["mos_bottom"] = mos_bottom.values
    out["mos_green"] = mos_green.values
    out["rsi_golden"] = rsi_golden.values
    out["bull_ma"] = bull_ma.values

    # 可变规则：枚举所有候选阈值
    for s in grid["macd.near_size"]:
        near = (~golden) & (diff < dea) & ((dea - diff) < s) & (diff > diff.shift(1))
        out[f"near_{s}"] = near.values
    for p in grid["breakout.period"]:
        prev_high = high.rolling(p).max().shift(1)
        out[f"breakout_{p}"] = (golden & (close > prev_high)).values
    for t in grid["drawdown.threshold"]:
        dd_hi = high.rolling(120).max().shift(1)
        out[f"drawdown_{t}"] = ((dd_hi > 0) & ((dd_hi - close) / dd_hi >= t)).values
    for r in grid["low_pos.ratio"]:
        seg_hi = high.rolling(120).max().shift(1)
        seg_lo = low.rolling(120).min().shift(1)
        out[f"lowpos_{r}"] = (close <= (seg_hi + seg_lo) * r).values

    # 前向收益
    out[f"ret_{horizon}"] = (close.shift(-horizon) / close - 1).values
    out["warmup"] = np.arange(n) >= WARMUP
    return out


def grid_search(sample: int = 400, horizon: int = 20, grid: dict | None = None,
                workers: int = 16, seed: int = 42) -> dict:
    """网格搜索：对参数组合按超额收益排序，并给出单参数敏感度。"""
    import itertools
    import random

    grid = grid or GRID_DEFAULT
    params = get_params()
    codes = _get_universe_codes()
    if sample and sample < len(codes):
        rng = random.Random(seed)
        codes = rng.sample(codes, sample)
    print(f"== 参数网格扫描 == 抽样 {len(codes)} 只，持有 {horizon} 天，"
          f"配置数 {np.prod([len(v) for v in grid.values()])}")
    t0 = time.time()

    pieces: list[pd.DataFrame] = []
    done = 0
    lock = threading.Lock()

    def _work(c):
        try:
            return _rich_flags(c, params, grid, horizon)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_work, c) for c in codes]
        for f in futs:
            d = f.result()
            with lock:
                done += 1
                if done % 100 == 0 or done == len(codes):
                    print(f"  预计算 {done}/{len(codes)}  耗时 {time.time()-t0:.0f}s")
            if d is not None and len(d) > 0:
                pieces.append(d)
    if not pieces:
        return {"configs": [], "sensitivity": {}, "sample": 0, "horizon": horizon}

    pool = pd.concat(pieces, ignore_index=True)
    ret_col = f"ret_{horizon}"
    valid = pool["warmup"] & pool[ret_col].notna()
    baseline = float(pool.loc[valid, ret_col].mean())
    print(f"预计算完成：{len(pool)} 行，基准收益 {_fmt_pct(baseline)}，耗时 {time.time()-t0:.0f}s")

    # 权重（固定，来自 params）
    w = {
        "golden": float(params["macd"]["golden_score"]),
        "near": float(params["macd"]["near_score"]),
        "green": float(params["macd"]["green_shrink_score"]),
        "mos_bottom": float(params["mos"]["bottom_score"]),
        "mos_green": float(params["mos"]["green_shrink_score"]),
        "breakout": float(params["breakout"]["score"]),
        "rsi_golden": float(params["rsi"]["score"]),
        "bull_ma": float(params["bull_ma"]["score"]),
        "low_pos": float(params["low_pos"]["score"]),
        "drawdown": float(params["drawdown"]["score"]),
    }

    fixed_score = (
        pool["golden"] * w["golden"] + pool["green"] * w["green"]
        + pool["mos_bottom"] * w["mos_bottom"] + pool["mos_green"] * w["mos_green"]
        + pool["rsi_golden"] * w["rsi_golden"] + pool["bull_ma"] * w["bull_ma"]
    )

    configs: list[dict] = []
    keys = list(grid.keys())
    # grid 长键名 -> config 短键名
    short_keys = ["near_size", "breakout_period", "drawdown_threshold", "low_pos_ratio"]
    for combo in itertools.product(*[grid[k] for k in keys]):
        s, p, t, r = combo  # near_size, breakout_period, dd_threshold, lp_ratio
        score = (fixed_score
                 + pool[f"near_{s}"] * w["near"]
                 + pool[f"breakout_{p}"] * w["breakout"]
                 + pool[f"drawdown_{t}"] * w["drawdown"]
                 + pool[f"lowpos_{r}"] * w["low_pos"])
        sig = valid & (score > 0)
        n = int(sig.sum())
        if n == 0:
            continue
        rets = pool.loc[sig, ret_col]
        mean_ret = float(rets.mean())
        configs.append({
            "near_size": s, "breakout_period": p, "drawdown_threshold": t, "low_pos_ratio": r,
            "n": n, "hit_rate": float((rets > 0).mean()), "mean_ret": mean_ret,
            "excess": mean_ret - baseline,
        })
    configs.sort(key=lambda x: -x["excess"])

    # 单参数敏感度：每个参数取值下，所有含该取值的配置的平均超额
    sensitivity: dict[str, list[dict]] = {}
    for gk, sk in zip(keys, short_keys, strict=True):
        by_val: dict[float, list[float]] = {}
        for c in configs:
            by_val.setdefault(c[sk], []).append(c["excess"])
        sensitivity[gk] = [
            {"value": v, "avg_excess": float(np.mean(xs)), "n_configs": len(xs)}
            for v, xs in sorted(by_val.items())
        ]

    return {
        "configs": configs, "sensitivity": sensitivity,
        "baseline_mean_ret": baseline, "sample": len(codes),
        "horizon": horizon, "grid": grid,
    }


def write_grid_report(report: dict) -> None:
    GRID_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    cfgs = report["configs"]
    lines: list[str] = []
    lines.append("# 玉姐精选参数网格扫描报告\n")
    lines.append(f"抽样股票：{report['sample']} 只\n")
    lines.append(f"持有期：{report['horizon']} 天\n")
    lines.append(f"基准收益（同股随机入场）：{_fmt_pct(report['baseline_mean_ret'])}\n")
    lines.append(f"配置总数：{len(cfgs)}\n\n")

    lines.append("## 一、Top 15 配置（按超额收益排序）\n")
    lines.append("| 即将金叉阈值 | 突破周期 | 回撤阈值 | 低位比例 | 信号数 | 命中率 | 平均收益 | 超额 |")
    lines.append("|-------------|---------|---------|---------|--------|--------|---------|------|")
    for c in cfgs[:15]:
        lines.append(
            f"| {c['near_size']} | {c['breakout_period']} | {c['drawdown_threshold']} | "
            f"{c['low_pos_ratio']} | {c['n']} | {c['hit_rate']*100:.1f}% | "
            f"{_fmt_pct(c['mean_ret'])} | **{_fmt_pct(c['excess'])}** |"
        )
    lines.append("")

    lines.append("## 二、单参数敏感度（平均超额）\n")
    for k, vals in report["sensitivity"].items():
        lines.append(f"### {k}\n")
        lines.append("| 取值 | 平均超额 | 配置数 |")
        lines.append("|------|---------|--------|")
        for v in vals:
            lines.append(f"| {v['value']} | {_fmt_pct(v['avg_excess'])} | {v['n_configs']} |")
        lines.append("")

    best = cfgs[0] if cfgs else None
    lines.append("## 结论\n")
    if best:
        lines.append(f"- 最优配置：即将金叉 {best['near_size']} / 突破 {best['breakout_period']} 日 / "
                     f"回撤 {best['drawdown_threshold']} / 低位 {best['low_pos_ratio']}，"
                     f"超额 {_fmt_pct(best['excess'])}（基准 {_fmt_pct(report['baseline_mean_ret'])}）。\n")
        lines.append("- 可将最优配置写入 config.json -> yujie 对应字段后重新扫描/回测验证。\n")
    GRID_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入：{GRID_REPORT_MD}")
    print(f"报告已写入：{GRID_REPORT_JSON}")


def print_grid_summary(report: dict) -> None:
    cfgs = report["configs"]
    print("\n" + "=" * 80)
    print(f"参数网格扫描汇总（抽样 {report['sample']} 只，持有 {report['horizon']} 天）")
    print(f"基准收益 {_fmt_pct(report['baseline_mean_ret'])}，配置数 {len(cfgs)}")
    print("=" * 80)
    print(f"{'即将金叉':>8} {'突破':>4} {'回撤':>5} {'低位':>5} {'信号数':>7} {'命中率':>6} {'平均':>8} {'超额':>8}")
    for c in cfgs[:10]:
        print(f"{c['near_size']:>8} {c['breakout_period']:>4} {c['drawdown_threshold']:>5} "
              f"{c['low_pos_ratio']:>5} {c['n']:>7} {c['hit_rate']*100:>5.1f}% "
              f"{_fmt_pct(c['mean_ret']):>8} {_fmt_pct(c['excess']):>8}")
    print("-" * 80)
    print("单参数敏感度（平均超额）:")
    for k, vals in report["sensitivity"].items():
        row = "  " + k + ": " + "  ".join(
            f"{v['value']}→{_fmt_pct(v['avg_excess'])}" for v in vals
        )
        print(row)
    print("=" * 80)


# ============================================================
# CLI
# ============================================================


def main():
    ap = argparse.ArgumentParser(description="玉姐精选评分回测")
    ap.add_argument("cmd", choices=["prepare", "backtest", "grid", "all"])
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 只股票（调试）")
    ap.add_argument("--workers", type=int, default=32, help="并发线程数")
    ap.add_argument("--datalen", type=int, default=1024, help="历史拉取天数")
    ap.add_argument("--sample", type=int, default=400, help="网格扫描抽样股票数")
    ap.add_argument("--horizon", type=int, default=20, help="网格扫描持有期")
    args = ap.parse_args()

    if args.cmd in ("prepare", "all"):
        prepare_history(limit=args.limit, workers=args.workers, datalen=args.datalen)
    if args.cmd in ("backtest", "all"):
        report = run_backtest(limit=args.limit, workers=max(8, args.workers // 2))
        write_report(report)
        print_summary(report)
    if args.cmd == "grid":
        report = grid_search(sample=args.sample, horizon=args.horizon,
                             workers=max(8, args.workers // 2))
        write_grid_report(report)
        print_grid_summary(report)


if __name__ == "__main__":
    main()
