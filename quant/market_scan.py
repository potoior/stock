"""全市场策略选股扫描: scan_with_strategy / scan_combo_strategies。

从 strategy_engine 拆出的市场级扫描层(单向依赖 strategy_engine,
后者不再反向引用本模块):
- _bulk_fetch_daily: 批量读缓存日K线(IN 查询,只读不联网)
- scan_with_strategy: 单策略全市场扫描
- scan_combo_strategies: 多策略组合扫描(AND 共振 / OR 宽松)

依赖方向(单向,无环): market_scan -> strategy_engine / data_fetcher / strategy_indicators
"""

import sqlite3
import time as _time
from datetime import datetime

import pandas as pd

from data_fetcher import CACHE_DB
from strategy_engine import BUILTIN_REGISTRY, BUILTIN_STRATEGY_IDS
from strategy_indicators import (
    DEFAULT_STRATEGY_PARAMS,
    compute_boll,
    compute_dmi,
    compute_macd,
    compute_rsi,
    compute_tower,
)

# id -> 策略评估函数(显式注册表,替代拆分前的 globals() 动态查找)
_REGISTRY_MAP = {sid: fn for sid, _name, fn in BUILTIN_REGISTRY}


_STRATEGY_DEPS: dict[str, tuple[str, ...]] = {
    "bottom_ma": ("ma10", "ma20", "ma5", "ma60"),
    "bottom_time": ("ma60",),
    "dragon_pullback": ("ma10",),
    "macd_top_divergence": ("macd_diff",),
    "macd_bottom_divergence": ("macd_diff",),
    "plan_trade": ("ma10", "macd_dea", "macd_diff"),
    "resonance": ("boll_m", "ma5"),
    "rsi_top_divergence": ("rsi6",),
    "rsi_bottom_divergence": ("rsi6",),
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


# ---------------- 全市场扫描共用(scan_with_strategy / scan_combo_strategies) ----------------

# BUILTIN_STRATEGY_IDS 已上移 strategy_engine(由 BUILTIN_REGISTRY 派生)

# 需要联网取外部数据的策略:全市场扫描每只都要联网,会跑数小时
NO_SCAN_STRATEGIES = {"shareholder_select", "policy_select"}
# 观察型策略:命中时只输出观察信息,永远不返回 buy/sell,
# 用于选股扫描必然 0 命中,应拒绝
NO_BUY_SIGNAL_STRATEGIES = {"zhuang_test", "zhuang_wash", "zt_pull"}


def _bulk_fetch_daily(candidates, days: int = 320) -> dict:
    """批量拉取候选股票的日 K 线(只读 sqlite,不联网)。

    供全市场扫描用:一次 IN 查询代替逐股 SELECT,约 14x 提速;
    且不经过 get_daily_data,避免缓存不新鲜时逐股联网刷新。
    Returns: {code: DataFrame(open/close/high/low/volume, 按日期升序, 最多 days 行)}
    """
    if not candidates:
        return {}
    from datetime import timedelta as _td

    # 交易日 → 日历日放大 1.6 倍取 cutoff,再按行数截断,与 get_daily_data(days) 口径一致
    date_cutoff = (datetime.now() - _td(days=int(days * 1.6))).strftime("%Y%m%d")
    grouped: dict = {}
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        codes = list(candidates)
        batch_size = 500  # 规避 sqlite IN 参数上限(默认 999)
        for k in range(0, len(codes), batch_size):
            batch = codes[k:k + batch_size]
            placeholders = ",".join("?" * len(batch))
            cur = conn.execute(
                f"SELECT code, date, open, close, high, low, volume FROM daily "
                f"WHERE code IN ({placeholders}) AND date >= ?",
                [*batch, date_cutoff],
            )
            cols = [d[0] for d in cur.description]
            df = pd.DataFrame(cur.fetchall(), columns=cols)
            if df.empty:
                continue
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d", errors="coerce")
            for code, g in df.groupby("code", sort=False):
                g = g.sort_values("date").reset_index(drop=True)
                if len(g) > days:
                    g = g.tail(days).reset_index(drop=True)
                grouped[code] = g
    finally:
        conn.close()
    return grouped


def scan_with_strategy(
    strategy_id: str,
    top_n: int = 20,
    min_amount_yi: float = 0.5,
    limit: int = 0,
    progress_callback=None,
) -> dict:
    """全市场扫描指定策略,返回触发 buy 信号的股票列表。

    轻量版:跳过 fetch_realtime,只跑指定单策略(非 analyze 的全部 56 个),
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
      - 只读 daily 表已缓存数据(批量拉取,不联网)
      - 跳过 ST/退市股
      - 单线程跑(策略函数非线程安全,参考 backtest_builtin workers=1)
      - 数据不足(< 60 日)跳过
    """
    import sqlite3

    t0 = _time.time()

    # 1. 验证策略 id
    if strategy_id not in BUILTIN_STRATEGY_IDS:
        return {"error": f"未知策略 id: {strategy_id},必须是内置策略之一"}
    if strategy_id in NO_SCAN_STRATEGIES:
        return {
            "error": f"策略 {strategy_id} 需要联网获取外部数据(股东人数/新闻),"
            "不适合全市场扫描,请用 analyze_with_strategy 分析个股"
        }
    if strategy_id in NO_BUY_SIGNAL_STRATEGIES:
        return {
            "error": f"策略 {strategy_id} 为观察型(试盘/洗盘/拉高型,只输出观察信息不产生买卖信号),"
            "不适合选股扫描"
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

    # 跳过 ST/退市(名字来自 stock_names sqlite 缓存,不联网;未命中缓存的保留)
    try:
        import stock_names as _sn

        _name_map = _sn.lookup_names(candidates)
        candidates = [
            c for c in candidates
            if "ST" not in (_name_map.get(c, "") or "") and "退" not in (_name_map.get(c, "") or "")
        ]
    except Exception:
        pass

    if limit and limit < len(candidates):
        candidates = candidates[:limit]

    # 3. 单线程跑指定策略(策略函数非线程安全)
    fn = _REGISTRY_MAP.get(strategy_id)
    if fn is None:
        return {"error": f"策略函数 strategy_{strategy_id} 不存在"}

    grouped = _bulk_fetch_daily(candidates, days=320)
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
            df = grouped.get(code)
            if df is None or len(df) < 60:
                continue
            close = df["close"]
            i = len(df) - 1
            price = float(close.iloc[i])

            # ST/退市已在候选池层过滤(见上 stock_names 缓存)

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


def scan_combo_strategies(
    strategy_ids: list[str],
    top_n: int = 20,
    min_amount_yi: float = 0.5,
    limit: int = 0,
    mode: str = "and",
    progress_callback=None,
) -> dict:
    """全市场多策略组合扫描,返回同时/任一触发 buy 信号的股票。

    多策略共振选股:单一策略噪音大,多条件交叉验证胜率高。

    Args:
        strategy_ids: 策略 id 列表(2-5 个)
        mode: and=全部触发(共振,信号少而精) / or=任一触发(宽松)
        其余同 scan_with_strategy

    Returns: {"strategies": [...], "mode", "scanned", "hits_count", "hits", "elapsed_sec"}
    """
    import sqlite3

    t0 = _time.time()

    # 1. 校验策略 id(复用单策略扫描的白名单)
    bad = [s for s in strategy_ids if s not in BUILTIN_STRATEGY_IDS]
    if bad:
        return {"error": f"未知策略 id: {bad},必须是内置策略"}
    need_net = [s for s in strategy_ids if s in NO_SCAN_STRATEGIES]
    if need_net:
        return {"error": f"策略 {need_net} 需联网,不适合全市场扫描"}
    inert = [s for s in strategy_ids if s in NO_BUY_SIGNAL_STRATEGIES]
    if inert:
        return {"error": f"策略 {inert} 为观察型(只返回 hold 不产生买卖信号),不适合选股扫描"}
    if not 2 <= len(strategy_ids) <= 5:
        return {"error": "策略数须 2-5 个"}
    if len(set(strategy_ids)) != len(strategy_ids):
        return {"error": "策略 id 不能重复"}
    if mode not in ("and", "or"):
        return {"error": f"mode 须为 and/or,收到: {mode}"}

    # 2. 候选池(同单策略扫描)
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        latest_date = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if not latest_date:
            conn.close()
            return {"error": "daily 表为空,请先运行 daily_scan 抓取数据"}
        if isinstance(latest_date, str):
            cutoff = (_dt.strptime(latest_date, "%Y%m%d") - _td(days=7)).strftime("%Y%m%d")
        else:
            cutoff = latest_date
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily WHERE date >= ?",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    candidates = [row[0] for row in rows]

    # 跳过 ST/退市(名字来自 stock_names sqlite 缓存,不联网;未命中缓存的保留)
    try:
        import stock_names as _sn

        _name_map = _sn.lookup_names(candidates)
        candidates = [
            c for c in candidates
            if "ST" not in (_name_map.get(c, "") or "") and "退" not in (_name_map.get(c, "") or "")
        ]
    except Exception:
        pass

    if limit and limit < len(candidates):
        candidates = candidates[:limit]

    # 3. 每只股票跑多策略(指标依赖取并集,只算一次)
    fns = []
    for sid in strategy_ids:
        fn = _REGISTRY_MAP.get(sid)
        if fn is None:
            return {"error": f"策略函数 strategy_{sid} 不存在"}
        fns.append((sid, fn))
    all_deps = set()
    for sid in strategy_ids:
        all_deps.update(_STRATEGY_DEPS.get(sid, ()))

    grouped = _bulk_fetch_daily(candidates, days=320)
    hits = []
    scanned = 0
    total = len(candidates)
    last_progress_at = 0
    for code in candidates:
        scanned += 1
        if scanned % 50 == 0:
            _time.sleep(0.001)
        if progress_callback and (scanned - last_progress_at >= 200 or scanned == total):
            try:
                progress_callback(scanned, total, len(hits))
            except Exception:
                pass
            last_progress_at = scanned
        try:
            df = grouped.get(code)
            if df is None or len(df) < 60:
                continue
            close = df["close"]
            i = len(df) - 1
            price = float(close.iloc[i])
            ctx = {
                "i": i, "price": price, "df": df, "close": close,
                "code": code, "realtime": None,
            }
            for ind in all_deps:
                ctx[ind] = _compute_indicator(df, ind)

            # 逐策略检查
            fired = []  # [(sid, reason)]
            for sid, fn in fns:
                params = {**DEFAULT_STRATEGY_PARAMS.get(sid, {})}
                sg, reason = fn(ctx, params)
                if sg == "buy":
                    fired.append((sid, reason))

            if mode == "and":
                if len(fired) != len(fns):
                    continue
            else:  # or
                if not fired:
                    continue

            # 成交额 + 涨幅过滤
            last_row = df.iloc[i]
            prev_close = float(close.iloc[i - 1]) if i >= 1 else price
            pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0
            amount_yi = float(last_row["volume"] * price) / 1e8 if "volume" in df.columns else 0
            if amount_yi < min_amount_yi:
                continue

            hits.append({
                "code": code,
                "name": "",
                "price": round(price, 2),
                "pct": round(pct, 2),
                "signals": [sid for sid, _ in fired],
                "reason": "; ".join(f"{sid}: {r}" for sid, r in fired),
                "amount_yi": round(amount_yi, 2),
            })
        except Exception:
            continue

    hits.sort(key=lambda x: x["pct"], reverse=True)
    hits = hits[:top_n]

    return {
        "strategies": strategy_ids,
        "mode": mode,
        "scanned": scanned,
        "hits_count": len(hits),
        "hits": hits,
        "elapsed_sec": round(_time.time() - t0, 1),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "scan":
        print(scan_with_strategy(sys.argv[2], top_n=20))
    elif len(sys.argv) > 2 and sys.argv[1] == "combo":
        print(scan_combo_strategies(sys.argv[2].split(","), top_n=20))
    else:
        print("用法: python market_scan.py scan <strategy_id> | combo <id,id,...>")
