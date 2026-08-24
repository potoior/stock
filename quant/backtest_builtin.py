"""内置策略批量回测：验证 19 个内置策略是否具有超额收益。

思路：
  1. _indicators():  对单只股票一次性算出所有指标(MACD/KDJ/BOLL/PSY/BIAS/DMI/SAR/BBIBOLL/TOWER/MA)
  2. signal_xxx():   每个策略一个向量化买入信号函数(返回 bool Series,事件触发避免信号堆叠)
  3. run_backtest(): 信号日收盘买入,持有 N 天统计前向收益;与"同股随机入场"基准对比
  4. grid_search():  对 MACD/KDJ/BOLL/DMI 关键参数网格寻优

用法：
  python backtest_builtin.py backtest             # 跑全部策略回测
  python backtest_builtin.py backtest --limit 200 # 调试:只跑前 200 只
  python backtest_builtin.py grid                 # 参数网格寻优
  python backtest_builtin.py all                  # 回测 + 网格
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

from strategy_engine import CACHE_DB  # noqa: F401  (re-export 兼容)
from strategy_indicators import (
    DEFAULT_STRATEGY_PARAMS,  # noqa: F401  (re-export 兼容)
    compute_bbiboll,
    compute_bias,
    compute_boll,
    compute_dmi,
    compute_kdj,
    compute_macd,
    compute_psy,
    compute_sar,
    compute_tower,
)

HOME = Path(__file__).parent
REPORT_MD = HOME / "builtin_backtest_report.md"
REPORT_JSON = HOME / "builtin_backtest_report.json"
GRID_REPORT_MD = HOME / "builtin_grid_report.md"
GRID_REPORT_JSON = HOME / "builtin_grid_report.json"

WARMUP = 120
HORIZONS = (5, 10, 20, 60)
MIN_DAYS = 200


# ============================================================
# 1. 一次性指标计算
# ============================================================


def _indicators(df: pd.DataFrame) -> dict:
    """对单只股票一次性算出所有策略所需指标,返回 dict of pd.Series。"""
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    diff, dea, bar = compute_macd(df)
    k, d, j, _ = compute_kdj(df)
    boll_u, boll_m, boll_l = compute_boll(df)
    psy = compute_psy(df)
    bias1, bias2, bias3 = compute_bias(df)
    pdi, mdi, adx = compute_dmi(df)
    sar, _trend = compute_sar(df)
    bbu, bbm, bbl = compute_bbiboll(df)
    tower = compute_tower(df)

    return {
        "close": close, "high": high, "low": low, "volume": volume,
        "diff": diff, "dea": dea, "bar": bar,
        "k": k, "d": d, "j": j,
        "boll_u": boll_u, "boll_m": boll_m, "boll_l": boll_l,
        "psy": psy, "bias1": bias1, "bias2": bias2, "bias3": bias3,
        "pdi": pdi, "mdi": mdi, "adx": adx,
        "sar": pd.Series(sar, index=df.index),
        "bbu": bbu, "bbm": bbm, "bbl": bbl,
        "tower": tower,
        "ma5": close.rolling(5).mean(),
        "ma10": close.rolling(10).mean(),
        "ma20": close.rolling(20).mean(),
        "ma60": close.rolling(60).mean(),
        "ma7": close.rolling(7).mean(),
        "ma13": close.rolling(13).mean(),
    }


# ============================================================
# 2. 向量化买入信号(每个策略一个函数,返回 bool Series)
# ============================================================


def _cross_up(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """fast 上穿 slow(金叉事件)。"""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))


def _cross_down(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """fast 下穿 slow(死叉事件)。"""
    return (fast < slow) & (fast.shift(1) >= slow.shift(1))


def signal_macd(inds: dict, p: dict) -> pd.Series:
    """MACD 金叉事件(零上+零下统一)。"""
    return _cross_up(inds["diff"], inds["dea"])


def signal_kdj(inds: dict, p: dict) -> pd.Series:
    """KDJ K 上穿 D(金叉)。"""
    return _cross_up(inds["k"], inds["d"])


def signal_ma_stop(inds: dict, p: dict) -> pd.Series:
    """价格上穿 MA5。"""
    period = int(p.get("period", 5))
    ma = inds["close"].rolling(period).mean()
    return _cross_up(inds["close"], ma)


def signal_boll(inds: dict, p: dict) -> pd.Series:
    """价格触及下轨(跌破下轨事件)。"""
    return (inds["close"] <= inds["boll_l"]) & (inds["close"].shift(1) > inds["boll_l"].shift(1))


def signal_dmi(inds: dict, p: dict) -> pd.Series:
    """+DI 上穿 -DI 且 ADX>20(多头趋势确立)。"""
    return _cross_up(inds["pdi"], inds["mdi"]) & (inds["adx"] > 20)


def signal_psy(inds: dict, p: dict) -> pd.Series:
    """PSY 跌破 25(进入超卖区事件)。"""
    return (inds["psy"] <= 25) & (inds["psy"].shift(1) > 25)


def signal_bias(inds: dict, p: dict) -> pd.Series:
    """BIAS6 跌破 -3%(超跌事件)。"""
    short = float(p.get("short", 3))
    return (inds["bias1"] <= -short) & (inds["bias1"].shift(1) > -short)


def signal_sar(inds: dict, p: dict) -> pd.Series:
    """价格上穿 SAR(翻红事件)。"""
    return _cross_up(inds["close"], inds["sar"])


def signal_bbiboll(inds: dict, p: dict) -> pd.Series:
    """价格跌破 BBIBOLL 下轨(极端超跌事件)。"""
    return (inds["close"] <= inds["bbl"]) & (inds["close"].shift(1) > inds["bbl"].shift(1))


def signal_tower(inds: dict, p: dict) -> pd.Series:
    """宝塔线翻红(从绿转红)。"""
    return (inds["tower"] == 1) & (inds["tower"].shift(1) == -1)


def signal_ma_combo(inds: dict, p: dict) -> pd.Series:
    """均线多头排列首次形成(price>ma_s>ma_m>ma_l 且前一日不满足)。"""
    short, mid, long_ = int(p.get("short", 5)), int(p.get("mid", 10)), int(p.get("long", 60))
    ma_s = inds["close"].rolling(short).mean()
    ma_m = inds["close"].rolling(mid).mean()
    ma_l = inds["close"].rolling(long_).mean()
    bull = (inds["close"] > ma_s) & (ma_s > ma_m) & (ma_m > ma_l)
    prev_bull = bull.shift(1).fillna(False).astype(bool)
    return bull & ~prev_bull


def signal_two_line(inds: dict, p: dict) -> pd.Series:
    """MA5 上穿 MA10。"""
    short, long_ = int(p.get("short", 5)), int(p.get("long", 10))
    ma_s = inds["close"].rolling(short).mean()
    ma_l = inds["close"].rolling(long_).mean()
    return _cross_up(ma_s, ma_l)


def signal_life_line(inds: dict, p: dict) -> pd.Series:
    """价格上穿 60 日生命线。"""
    period = int(p.get("period", 60))
    ma = inds["close"].rolling(period).mean()
    return _cross_up(inds["close"], ma)


def signal_three_third(inds: dict, p: dict) -> pd.Series:
    """站上三线(7/13/20)首次。"""
    p1, p2, p3 = int(p.get("p1", 7)), int(p.get("p2", 13)), int(p.get("p3", 20))
    ma1 = inds["close"].rolling(p1).mean()
    ma2 = inds["close"].rolling(p2).mean()
    ma3 = inds["close"].rolling(p3).mean()
    above = (inds["close"] > ma1) & (inds["close"] > ma2) & (inds["close"] > ma3)
    prev_above = above.shift(1).fillna(False).astype(bool)
    return above & ~prev_above


def signal_sparrow(inds: dict, p: dict) -> pd.Series:
    """麻雀战术是止盈策略,无买入信号 → 返回全 False。"""
    return pd.Series(False, index=inds["close"].index)


def signal_bounce(inds: dict, p: dict) -> pd.Series:
    """反弹量化:昨日下跌 + 今日涨幅>昨日跌幅*rebound_pct + 放量>vol_increase%。"""
    rebound_pct = float(p.get("rebound_pct", 0.5))
    vol_increase = float(p.get("vol_increase", 20))
    close = inds["close"]
    vol = inds["volume"]
    pc = close.shift(1)
    pp = close.shift(2)
    dc = (pc - pp) / pp * 100          # 昨日涨跌
    tc = (close - pc) / pc * 100        # 今日涨跌
    vr = (vol - vol.shift(1)) / vol.shift(1) * 100
    return (dc < 0) & (tc > (-dc) * rebound_pct) & (vr > vol_increase)


def signal_volume_div(inds: dict, p: dict) -> pd.Series:
    """量价背离:放量突破近 N 日新高。"""
    lookback = int(p.get("lookback", 10))
    shrink = float(p.get("shrink", 0.7))
    expand = float(p.get("expand", 1.3))
    close = inds["close"]
    vol = inds["volume"]
    rh = close.rolling(lookback).max().shift(1)
    avg = vol.rolling(lookback).mean().shift(1)
    return (close >= rh) & (vol > avg * expand) & (vol > avg * shrink)


def signal_resonance(inds: dict, p: dict) -> pd.Series:
    """三指标共振:MACD 金叉 + KDJ 金叉 + 站上 BOLL 中轨 + 站上 MA5(同日)。"""
    macd_golden = _cross_up(inds["diff"], inds["dea"])
    kdj_golden = _cross_up(inds["k"], inds["d"])
    return macd_golden & kdj_golden & (inds["close"] > inds["boll_m"]) & (inds["close"] > inds["ma5"])


def signal_dmi_psy(inds: dict, p: dict) -> pd.Series:
    """DMI+PSY 超跌:PDI<5 且 PSY<=25。"""
    pdi_threshold = float(p.get("pdi_threshold", 5))
    psy_threshold = float(p.get("psy_threshold", 25))
    return (inds["pdi"] < pdi_threshold) & (inds["psy"] <= psy_threshold)


# 策略注册表:(id, name, signal_fn)
SIGNAL_FNS = [
    ("macd", "MACD金叉死叉", signal_macd),
    ("kdj", "KDJ超买超卖", signal_kdj),
    ("ma_stop", "均线止损", signal_ma_stop),
    ("boll", "BOLL布林线", signal_boll),
    ("dmi", "DMI趋势", signal_dmi),
    ("psy", "PSY心理线", signal_psy),
    ("bias", "BIAS乖离率", signal_bias),
    ("sar", "SAR止损", signal_sar),
    ("bbiboll", "BBIBOLL多空布林", signal_bbiboll),
    ("tower", "宝塔线TOWER", signal_tower),
    ("ma_combo", "均线组合", signal_ma_combo),
    ("two_line", "二线法", signal_two_line),
    ("life_line", "60日生命线", signal_life_line),
    ("three_third", "三分法", signal_three_third),
    ("sparrow", "麻雀战术", signal_sparrow),
    ("bounce", "反弹量化", signal_bounce),
    ("volume_div", "量价背离", signal_volume_div),
    ("resonance", "三指标共振", signal_resonance),
    ("dmi_psy", "DMI+PSY超跌", signal_dmi_psy),
]


# ============================================================
# 3. 回测主循环
# ============================================================


def _get_universe_codes() -> list[str]:
    conn = sqlite3.connect(str(CACHE_DB))
    rows = conn.execute("SELECT DISTINCT code FROM daily ORDER BY code").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _load_code(code: str) -> pd.DataFrame | None:
    conn = sqlite3.connect(str(CACHE_DB), timeout=10)
    try:
        df = pd.read_sql(
            "SELECT date, open, close, high, low, volume FROM daily WHERE code=? ORDER BY date",
            conn, params=(code,),
        )
    finally:
        conn.close()
    if len(df) < MIN_DAYS:
        return None
    return df.sort_values("date").reset_index(drop=True)


def run_backtest(limit: int = 0, workers: int = 16, sample: int = 0) -> dict:
    """对 19 个内置策略批量回测,返回每策略的整体表现 + 基准对比。

    为控制内存,采用分批策略:每批处理完即把 sig_rets 转为 numpy 数组,
    避免大列表长期驻留。workers>1 时某些 compute_* 函数非线程安全,
    建议用 workers=1。
    """
    codes = _get_universe_codes()
    if limit:
        codes = codes[:limit]
    if sample and sample < len(codes):
        import random
        rng = random.Random(42)
        codes = rng.sample(codes, sample)
    print(f"== 内置策略回测 == 股票 {len(codes)} 只,策略 {len(SIGNAL_FNS)} 个,持有期 {HORIZONS} 天")
    t0 = time.time()

    # 每策略:信号前向收益(用 list 累积,完成后转 np.array 释放)
    sig_rets: dict[str, dict[int, list[float]]] = {sid: {h: [] for h in HORIZONS} for sid, _, _ in SIGNAL_FNS}
    sig_count: dict[str, int] = {sid: 0 for sid, _, _ in SIGNAL_FNS}
    baseline_acc = {h: [0.0, 0] for h in HORIZONS}

    done = 0

    def _process_one(c):
        df = _load_code(c)
        if df is None:
            return None
        try:
            inds = _indicators(df)
        except Exception:
            return None
        return inds

    for c in codes:
        inds = _process_one(c)
        done += 1
        if done % 200 == 0 or done == len(codes):
            print(f"  {done}/{len(codes)}  耗时 {time.time()-t0:.0f}s", flush=True)
        if inds is None:
            continue
        closes = inds["close"].values
        n = len(closes)
        # 各策略信号
        for sid, _name, fn in SIGNAL_FNS:
            params = DEFAULT_STRATEGY_PARAMS.get(sid, {})
            try:
                sig = fn(inds, params)
            except Exception:
                continue
            sig_arr = sig.values
            for i in range(WARMUP, n):
                if not sig_arr[i]:
                    continue
                sig_count[sid] += 1
                for h in HORIZONS:
                    j = i + h
                    if j < n:
                        sig_rets[sid][h].append(float(closes[j] / closes[i] - 1))
        # 基准:同股全日期(预热后)随机入场平均前向收益
        for h in HORIZONS:
            for i in range(WARMUP, n - h):
                baseline_acc[h][0] += closes[i + h] / closes[i] - 1
                baseline_acc[h][1] += 1
        # 释放 inds 引用
        del inds

    print(f"回测完成,耗时 {time.time()-t0:.0f}s")
    return _aggregate(sig_rets, sig_count, baseline_acc)


def _aggregate(sig_rets: dict, sig_count: dict, baseline_acc: dict) -> dict:
    """按策略汇总前向收益、命中率、超额。键统一用字符串(便于 JSON 序列化和前端读取)。"""
    report = {"strategies": {}, "horizons": [h for h in HORIZONS]}
    base = {str(h): (baseline_acc[h][0] / baseline_acc[h][1] if baseline_acc[h][1] else 0.0)
            for h in HORIZONS}
    report["baseline"] = base

    for sid, name, _fn in SIGNAL_FNS:
        st = {"id": sid, "name": name, "signal_count": sig_count[sid], "horizons": {}}
        for h in HORIZONS:
            rets = sig_rets[sid][h]
            if not rets:
                continue
            arr = np.array(rets)
            st["horizons"][str(h)] = {
                "n": int(len(arr)),
                "hit_rate": float((arr > 0).mean()),
                "mean_ret": float(arr.mean()),
                "median_ret": float(np.median(arr)),
                "excess": float(arr.mean() - base[str(h)]),
            }
        report["strategies"][sid] = st
    return report


# ============================================================
# 4. 报告输出
# ============================================================


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def write_report(report: dict) -> None:
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    horizons = report["horizons"]
    lines: list[str] = []
    lines.append("# 内置策略批量回测报告\n")
    lines.append(f"持有期：{horizons} 天（信号日收盘买入,持有 N 天收盘卖出）\n")
    lines.append("基准：同股随机入场平均前向收益\n")
    lines.append(f"策略数：{len(report['strategies'])}\n\n")

    # 基准
    lines.append("## 基准收益（同股随机入场）\n")
    lines.append("| 持有期 | 基准收益 |")
    lines.append("|--------|---------|")
    for h in horizons:
        lines.append(f"| {h}天 | {_fmt_pct(report['baseline'][str(h)])} |")
    lines.append("")

    # 按策略一表汇总(以 20 天持有期为排序基准)
    lines.append("## 各策略表现（按 20 天超额降序）\n")
    lines.append("| 策略 | 信号数 | 5天超额 | 10天超额 | 20天超额 | 60天超额 | 20天命中率 | 20天平均 |")
    lines.append("|------|--------|---------|---------|---------|---------|-----------|---------|")
    strats = list(report["strategies"].values())
    strats.sort(key=lambda s: -s["horizons"].get("20", {}).get("excess", -999))
    for s in strats:
        h = s["horizons"]
        def _fmt_ex(d, key):
            v = d.get(str(key), {})
            return _fmt_pct(v["excess"]) if v else "-"
        e5 = _fmt_ex(h, 5)
        e10 = _fmt_ex(h, 10)
        e20 = _fmt_ex(h, 20)
        e60 = _fmt_ex(h, 60)
        h20 = h.get("20", {})
        hr = f"{h20['hit_rate']*100:.1f}%" if h20 else "-"
        mr = _fmt_pct(h20["mean_ret"]) if h20 else "-"
        lines.append(f"| {s['name']} | {s['signal_count']} | {e5} | {e10} | {e20} | {e60} | {hr} | {mr} |")
    lines.append("")

    # 各策略分持有期详情
    lines.append("## 各策略分持有期详情\n")
    for s in strats:
        lines.append(f"### {s['name']}（信号 {s['signal_count']} 条）\n")
        lines.append("| 持有期 | 样本数 | 命中率 | 平均收益 | 中位收益 | 基准 | 超额 |")
        lines.append("|--------|--------|--------|---------|---------|------|------|")
        for h in horizons:
            r = s["horizons"].get(str(h))
            if not r:
                continue
            lines.append(
                f"| {h}天 | {r['n']} | {r['hit_rate']*100:.1f}% | "
                f"{_fmt_pct(r['mean_ret'])} | {_fmt_pct(r['median_ret'])} | "
                f"{_fmt_pct(report['baseline'][str(h)])} | **{_fmt_pct(r['excess'])}** |"
            )
        lines.append("")

    lines.append("## 结论要点\n")
    lines.append("- 超额稳定为正 → 该策略有 alpha；持续为负 → 信号反向或失效。\n")
    lines.append("- 信号数过少(<100)的统计意义弱,需谨慎解读。\n")
    lines.append("- 麻雀战术是止盈策略,无买入信号,信号数为 0 属正常。\n")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入：{REPORT_MD}")
    print(f"报告已写入：{REPORT_JSON}")


def print_summary(report: dict) -> None:
    horizons = report["horizons"]
    print("\n" + "=" * 92)
    print(f"内置策略回测汇总（持有期 {horizons} 天）")
    print("=" * 92)
    print(f"{'策略':<18} {'信号数':>7} | " + " | ".join(
        f"{h}天超额".center(11) for h in horizons))
    print("-" * 92)
    strats = list(report["strategies"].values())
    strats.sort(key=lambda s: -s["horizons"].get("20", {}).get("excess", -999))
    for s in strats:
        cells = []
        for h in horizons:
            r = s["horizons"].get(str(h))
            cells.append(_fmt_pct(r["excess"]) if r else "-")
        print(f"{s['name']:<18} {s['signal_count']:>7} | " + " | ".join(c.center(11) for c in cells))
    print("=" * 92)


# ============================================================
# 5. 参数网格寻优(MACD/KDJ/BOLL/DMI)
# ============================================================


# 网格定义
GRID = {
    "macd": {"fast": [8, 10, 12], "slow": [20, 24, 26], "signal": [7, 9, 12]},
    "kdj": {"n": [7, 9, 14], "k1": [2, 3, 5], "d1": [2, 3, 5]},
    "boll": {"period": [10, 20, 30], "std": [1.5, 2.0, 2.5]},
    "dmi": {"n": [10, 14, 20], "m": [4, 6, 9]},
}


def _grid_signal_strat(inds: dict, sid: str, params: dict) -> pd.Series:
    """用给定参数重算单策略信号(供 grid 使用)。"""
    df_like = inds["close"].to_frame("close")
    df_like["high"] = inds["high"]
    df_like["low"] = inds["low"]
    df_like["volume"] = inds["volume"]
    if sid == "macd":
        diff, dea, _ = compute_macd(df_like, fast=int(params["fast"]), slow=int(params["slow"]),
                                    signal=int(params["signal"]))
        return _cross_up(diff, dea)
    if sid == "kdj":
        k, d, _, _ = compute_kdj(df_like, n=int(params["n"]), k1=int(params["k1"]), d1=int(params["d1"]))
        return _cross_up(k, d)
    if sid == "boll":
        u, m, lo = compute_boll(df_like, period=int(params["period"]), std=float(params["std"]))
        return (inds["close"] <= lo) & (inds["close"].shift(1) > lo.shift(1))
    if sid == "dmi":
        pdi, mdi, adx = compute_dmi(df_like, n=int(params["n"]), m=int(params["m"]))
        return _cross_up(pdi, mdi) & (adx > 20)
    raise ValueError(f"未知策略 {sid}")


def grid_search(sample: int = 400, horizon: int = 20, workers: int = 16) -> dict:
    """对 MACD/KDJ/BOLL/DMI 各自做参数网格寻优。"""
    import itertools
    import random

    codes = _get_universe_codes()
    if sample and sample < len(codes):
        rng = random.Random(42)
        codes = rng.sample(codes, sample)
    print(f"== 内置策略参数网格 == 抽样 {len(codes)} 只,持有 {horizon} 天,策略 {list(GRID.keys())}")
    t0 = time.time()

    # 预加载并算基础指标
    pieces: list[tuple] = []   # (closes, inds)
    done = 0
    lock = threading.Lock()

    def _work(c):
        df = _load_code(c)
        if df is None:
            return None
        try:
            inds = _indicators(df)
        except Exception:
            return None
        return inds["close"].values, inds

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_work, c) for c in codes]
        for f in futs:
            r = f.result()
            with lock:
                done += 1
                if done % 200 == 0 or done == len(codes):
                    print(f"  预加载 {done}/{len(codes)}  耗时 {time.time()-t0:.0f}s")
            if r is not None:
                pieces.append(r)
    if not pieces:
        return {"strategies": {}, "sample": 0, "horizon": horizon}

    # 基准
    all_rets = []
    for closes, _ in pieces:
        n = len(closes)
        for i in range(WARMUP, n - horizon):
            all_rets.append(closes[i + horizon] / closes[i] - 1)
    baseline = float(np.mean(all_rets)) if all_rets else 0.0
    print(f"预加载完成:{len(pieces)} 只,基准 {_fmt_pct(baseline)},耗时 {time.time()-t0:.0f}s")

    report = {"strategies": {}, "baseline": baseline, "sample": len(pieces), "horizon": horizon}

    for sid, grid_params in GRID.items():
        keys = list(grid_params.keys())
        configs: list[dict] = []
        for combo in itertools.product(*[grid_params[k] for k in keys]):
            params = dict(zip(keys, combo, strict=True))
            rets = []
            for closes, inds in pieces:
                try:
                    sig = _grid_signal_strat(inds, sid, params)
                except Exception:
                    continue
                sig_arr = sig.values
                n = len(closes)
                for i in range(WARMUP, n - horizon):
                    if sig_arr[i]:
                        rets.append(closes[i + horizon] / closes[i] - 1)
            if len(rets) < 20:  # 信号太少跳过
                continue
            arr = np.array(rets)
            configs.append({
                "params": params, "n": int(len(arr)),
                "hit_rate": float((arr > 0).mean()),
                "mean_ret": float(arr.mean()),
                "excess": float(arr.mean() - baseline),
            })
        configs.sort(key=lambda x: -x["excess"])
        # 单参数敏感度
        sens: dict[str, dict] = {}
        for k in keys:
            by_val: dict[float, list[float]] = {}
            for c in configs:
                by_val.setdefault(c["params"][k], []).append(c["excess"])
            sens[k] = {v: float(np.mean(xs)) for v, xs in by_val.items()}
        report["strategies"][sid] = {"configs": configs[:20], "sensitivity": sens,
                                     "grid": grid_params, "total_configs": len(configs)}
        print(f"  {sid}: {len(configs)} 配置,Top超额 "
              f"{_fmt_pct(configs[0]['excess']) if configs else '-'}")

    return report


# ============================================================
# 5. 多策略组合回测(AND/OR)
# ============================================================


def run_combo_backtest(
    strategy_ids: list[str],
    mode: str = "and",
    horizon: int = 20,
    sample: int = 400,
    workers: int = 1,
    progress_callback=None,
) -> dict:
    """多策略组合回测: AND=所有策略同时触发, OR=任一触发。

    Args:
        strategy_ids: 策略 id 列表(如 ["macd", "boll"])
        mode: "and"=所有策略同日触发, "or"=任一触发
        horizon: 持有期(天),默认 20
        sample: 抽样股票数,0=全市场
        workers: 并发线程数(策略函数非线程安全,建议 1)
        progress_callback: 可选回调 fn(scanned, total, hits_count),用于发进度提示

    Returns: {
        strategy_ids, mode, horizon, sample,
        signal_count, hit_rate, mean_ret, excess, baseline,
        per_strategy: {sid: {signal_count, hit_rate, mean_ret, excess}}
    }
    """
    import random

    # 验证策略 id
    fn_map = {sid: fn for sid, _, fn in SIGNAL_FNS}
    for sid in strategy_ids:
        if sid not in fn_map:
            return {"error": f"未知策略 id: {sid},必须是内置策略之一: {list(fn_map.keys())}"}
    if len(strategy_ids) < 2:
        return {"error": "组合回测需要至少 2 个策略"}
    if mode not in ("and", "or"):
        return {"error": f"mode 必须是 'and' 或 'or',实际 '{mode}'"}

    codes = _get_universe_codes()
    if sample and sample < len(codes):
        rng = random.Random(42)
        codes = rng.sample(codes, sample)
    print(f"== 组合回测 == 策略 {strategy_ids} [{mode}],抽样 {len(codes)} 只,持有 {horizon} 天")
    t0 = time.time()

    # 预加载并算指标
    pieces: list[tuple] = []  # (closes, inds)
    lock = threading.Lock()
    done = [0]

    def _work(c):
        df = _load_code(c)
        if df is None:
            return None
        try:
            inds = _indicators(df)
        except Exception:
            return None
        return inds["close"].values, inds

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
        futs = [ex.submit(_work, c) for c in codes]
        for f in futs:
            r = f.result()
            with lock:
                done[0] += 1
                if done[0] % 200 == 0 or done[0] == len(codes):
                    print(f"  预加载 {done[0]}/{len(codes)}  耗时 {time.time()-t0:.0f}s", flush=True)
                    if progress_callback:
                        try:
                            progress_callback(done[0], len(codes), len(pieces))
                        except Exception:
                            pass
            if r is not None:
                pieces.append(r)
    if not pieces:
        return {"error": "无有效股票数据"}

    # 基准:同股全日期随机入场平均前向收益
    all_rets = []
    for closes, _ in pieces:
        n = len(closes)
        for i in range(WARMUP, n - horizon):
            all_rets.append(closes[i + horizon] / closes[i] - 1)
    baseline = float(np.mean(all_rets)) if all_rets else 0.0

    # 算每个策略的信号
    per_strategy: dict[str, dict] = {sid: {"rets": [], "count": 0} for sid in strategy_ids}
    combo_rets: list[float] = []
    combo_count = 0

    for closes, inds in pieces:
        n = len(closes)
        # 预算各策略信号
        sigs = {}
        for sid in strategy_ids:
            params = DEFAULT_STRATEGY_PARAMS.get(sid, {})
            try:
                sigs[sid] = fn_map[sid](inds, params).values
            except Exception:
                sigs[sid] = np.zeros(n, dtype=bool)

        for i in range(WARMUP, n - horizon):
            ret = closes[i + horizon] / closes[i] - 1
            # 组合信号
            fires = [bool(sigs[sid][i]) for sid in strategy_ids]
            if mode == "and":
                combo_fire = all(fires)
            else:  # or
                combo_fire = any(fires)
            if combo_fire:
                combo_rets.append(ret)
                combo_count += 1
            # 各策略单独统计(zip 避免内层 O(N) list.index 查找)
            for sid, fire in zip(strategy_ids, fires, strict=False):
                if fire:
                    per_strategy[sid]["rets"].append(ret)
                    per_strategy[sid]["count"] += 1

    # 汇总
    def _stats(rets, count):
        if count == 0:
            return {"signal_count": 0, "hit_rate": 0.0, "mean_ret": 0.0, "excess": 0.0}
        arr = np.array(rets)
        return {
            "signal_count": count,
            "hit_rate": float((arr > 0).mean()),
            "mean_ret": float(arr.mean()),
            "excess": float(arr.mean() - baseline),
        }

    combo_stats = _stats(combo_rets, combo_count)
    per_stats = {sid: _stats(per_strategy[sid]["rets"], per_strategy[sid]["count"])
                 for sid in strategy_ids}

    elapsed = time.time() - t0
    print(f"组合回测完成: {mode.upper()} 模式,信号 {combo_count} 次,超额 {combo_stats['excess']*100:.2f}%,耗时 {elapsed:.0f}s")

    return {
        "strategy_ids": strategy_ids,
        "mode": mode,
        "horizon": horizon,
        "sample": len(pieces),
        "baseline": baseline,
        "combo": combo_stats,
        "per_strategy": per_stats,
        "elapsed_sec": elapsed,
    }


def write_grid_report(report: dict) -> None:
    GRID_REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines: list[str] = []
    lines.append("# 内置策略参数网格扫描报告\n")
    lines.append(f"抽样股票：{report['sample']} 只\n")
    lines.append(f"持有期：{report['horizon']} 天\n")
    lines.append(f"基准收益：{_fmt_pct(report['baseline'])}\n\n")

    for sid, srep in report["strategies"].items():
        lines.append(f"## {sid}（{srep['total_configs']} 配置）\n")
        lines.append("### Top 10 配置\n")
        keys = list(srep["grid"].keys())
        lines.append("| " + " | ".join(keys) + " | 信号数 | 命中率 | 平均 | 超额 |")
        lines.append("|" + "|".join(["---"] * (len(keys) + 4)) + "|")
        for c in srep["configs"][:10]:
            vals = [str(c["params"][k]) for k in keys]
            lines.append(
                "| " + " | ".join(vals) + f" | {c['n']} | {c['hit_rate']*100:.1f}% | "
                f"{_fmt_pct(c['mean_ret'])} | **{_fmt_pct(c['excess'])}** |"
            )
        lines.append("")
        lines.append("### 单参数敏感度\n")
        for k, sens in srep["sensitivity"].items():
            row = "  " + k + ": " + "  ".join(
                f"{v}→{_fmt_pct(e)}" for v, e in sorted(sens.items()))
            lines.append(row)
        lines.append("")
    GRID_REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已写入：{GRID_REPORT_MD}")
    print(f"报告已写入：{GRID_REPORT_JSON}")


# ============================================================
# CLI
# ============================================================


def main():
    ap = argparse.ArgumentParser(description="内置策略批量回测")
    ap.add_argument("cmd", choices=["backtest", "grid", "all"])
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 只股票(调试)")
    ap.add_argument("--sample", type=int, default=0, help="随机抽样 N 只(0=全量)")
    ap.add_argument("--workers", type=int, default=16, help="并发线程数")
    ap.add_argument("--horizon", type=int, default=20, help="网格扫描持有期")
    args = ap.parse_args()

    if args.cmd in ("backtest", "all"):
        report = run_backtest(limit=args.limit, workers=args.workers, sample=args.sample)
        write_report(report)
        print_summary(report)
    if args.cmd in ("grid", "all"):
        report = grid_search(sample=args.sample or 400, horizon=args.horizon,
                              workers=args.workers)
        write_grid_report(report)


if __name__ == "__main__":
    main()
