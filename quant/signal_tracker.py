"""策略信号绩效跟踪: 记录每日信号 -> 评估 5/20 日收益 -> 每周战报。

采集点: watchlist_check.py 每日 15:30 分析持仓+自选时,把每个策略的
买卖信号落库(同日同股同策略去重)。评估(回填收益)在每周战报生成前执行。
"""

import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).parent
SIGNALS_DB = BASE / "signals.db"

# 评估窗口: N 个交易日后的收益率
EVAL_HORIZONS = (5, 20)


def _db():
    """信号 sqlite。启用 WAL 防多进程并发锁竞争。"""
    conn = sqlite3.connect(str(SIGNALS_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            date TEXT,
            code TEXT,
            name TEXT,
            strategy TEXT,
            direction TEXT,
            price REAL,
            ret_5d REAL,
            ret_20d REAL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup "
        "ON signals(date, code, strategy, direction)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date)"
    )
    conn.commit()
    return conn


def record_signals(code: str, result: dict) -> int:
    """记录一次分析结果中的买卖信号,返回写入条数(去重后)。

    result 为 strategy_engine.analyze() 的返回值,取 buy_reasons/sell_reasons,
    每条 {name: 策略名, reason: 理由}。
    """
    today = time.strftime("%Y%m%d")
    buys = result.get("buy_reasons") or []
    sells = result.get("sell_reasons") or []
    if not buys and not sells:
        return 0
    rt = result.get("realtime") or {}
    name = rt.get("name", "")
    price = float(rt.get("price") or 0)
    rows = []
    for sig in buys:
        rows.append((sig.get("name", ""), "buy"))
    for sig in sells:
        rows.append((sig.get("name", ""), "sell"))
    written = 0
    try:
        conn = _db()
        for strategy, direction in rows:
            try:
                conn.execute(
                    "INSERT INTO signals(ts, date, code, name, strategy, direction, price) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (int(time.time()), today, code, name, strategy, direction, price),
                )
                written += 1
            except sqlite3.IntegrityError:
                pass  # 同日同股同策略同方向已记录
        conn.commit()
        conn.close()
    except Exception:
        pass
    return written


def evaluate_pending(limit: int = 200) -> int:
    """回填信号的 5/20 日收益,返回评估条数。

    基准: 信号日收盘价;不足 N 个交易日的信号跳过(留待下次)。
    """
    conn = _db()
    rows = conn.execute(
        f"SELECT id, date, code, strategy, direction FROM signals "
        f"WHERE (ret_5d IS NULL OR ret_20d IS NULL) ORDER BY id LIMIT {int(limit)}"
    ).fetchall()
    if not rows:
        conn.close()
        return 0

    from data_fetcher import get_daily_data

    # 按股分组,一只股只拉一次 K 线
    codes = list({r[2] for r in rows})
    dfs = {}
    for c in codes:
        try:
            dfs[c] = get_daily_data(c)
        except Exception:
            pass

    evaluated = 0
    for sig_id, date, code, _, _ in rows:
        df = dfs.get(code)
        if df is None or df.empty:
            continue
        try:
            dates = df["date"].astype(str).tolist()
            if date not in dates:
                continue
            base_i = dates.index(date)
            closes = df["close"].tolist()
            update = {}
            for h in EVAL_HORIZONS:
                col = f"ret_{h}d"
                end_i = base_i + h
                if end_i >= len(closes):
                    continue  # 数据不足,留待下次
                update[col] = closes[end_i] / closes[base_i] - 1
            if not update:
                continue
            sets = ", ".join(f"{k}=?" for k in update)
            conn.execute(
                f"UPDATE signals SET {sets} WHERE id=?",
                [*update.values(), sig_id],
            )
            evaluated += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return evaluated


def weekly_summary(weeks: int = 1) -> str:
    """最近 N 周信号绩效统计文本(供每周战报调用)。"""
    conn = _db()
    since = int(time.time()) - weeks * 7 * 86400
    rows = conn.execute(
        "SELECT code, name, strategy, direction, date, ret_5d, ret_20d FROM signals "
        "WHERE ts >= ? ORDER BY date",
        (since,),
    ).fetchall()
    conn.close()
    if not rows:
        return "📉 近一周无信号记录"

    # 按策略聚合: 5 日收益(信号发出 5 个交易日后的涨跌幅)
    stats = {}  # strategy -> {total, win, rets, dir}
    for _, _, strategy, direction, _, ret5, _ in rows:
        s = stats.setdefault(strategy, {"total": 0, "win": 0, "rets": [], "dir": set()})
        s["total"] += 1
        s["dir"].add(direction)
        if ret5 is not None:
            s["rets"].append(ret5)
            if (ret5 > 0 and direction == "buy") or (ret5 < 0 and direction == "sell"):
                s["win"] += 1
    ranked = []
    for strategy, s in stats.items():
        avg = sum(s["rets"]) / len(s["rets"]) if s["rets"] else None
        win = s["win"] / len(s["rets"]) * 100 if s["rets"] else None
        ranked.append((strategy, s, avg, win))
    ranked.sort(key=lambda x: -(x[2] or 0))

    lines = [f"📊 策略信号绩效(近 {weeks} 周,共 {len(rows)} 条信号)"]
    for strategy, s, avg, win in ranked:
        evaluated_n = len(s["rets"])
        direction_cn = "/".join(sorted(d for d in s["dir"]))
        if avg is None:
            lines.append(f"• {strategy}({direction_cn}): {s['total']} 条信号,待评估")
        else:
            lines.append(
                f"• {strategy}({direction_cn}): {s['total']} 条信号,"
                f"已评估 {evaluated_n} 条,5日平均收益 {avg:+.2%},胜率 {win:.0f}%"
            )
    return "\n".join(lines)
