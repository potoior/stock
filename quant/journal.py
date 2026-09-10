"""交易日记: 记录买卖决策,自动关联后续走势。

用户说"记一笔/帮我记一下"时落库,查询时附带实时盈亏。
数据按 session_id 隔离(群内共享本群记录,私聊个人)。
"""

import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).parent
JOURNAL_DB = BASE / "journal.db"

ACTION_CN = {"buy": "买入", "sell": "卖出", "note": "笔记"}


def _db():
    """日记 sqlite。启用 WAL 防多进程并发锁竞争。"""
    conn = sqlite3.connect(str(JOURNAL_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            session_id TEXT,
            action TEXT,
            code TEXT,
            name TEXT,
            price REAL,
            qty REAL,
            note TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_session ON journal(session_id, ts)")
    conn.commit()
    return conn


def journal_add(session_id: str, action: str, code: str, name: str = "",
                price: float = 0, qty: float = 0, note: str = "") -> str:
    """记录一笔交易日记。action: buy/sell/note"""
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO journal(ts, session_id, action, code, name, price, qty, note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (int(time.time()), session_id, action, code, name, float(price), float(qty), note),
        )
        conn.commit()
        conn.close()
        action_cn = ACTION_CN.get(action, action)
        parts = [f"✅ 已记录{action_cn} {name or code}({code})"]
        if price > 0:
            parts.append(f"价 {price:g}")
        if qty > 0:
            parts.append(f"{qty:g}股")
        if note:
            parts.append(f"备注: {note}")
        return " ".join(parts)
    except Exception as e:
        return f"❌ 记录失败: {e}"


def journal_rows(session_id: str, days: int = 0, code: str = "") -> list[dict]:
    """查询日记。days=最近 N 天(0=全部),code 过滤。"""
    try:
        conn = _db()
        sql = "SELECT ts, action, code, name, price, qty, note, id FROM journal WHERE session_id=?"
        params = [session_id]
        if days > 0:
            sql += " AND ts >= ?"
            params.append(int(time.time()) - days * 86400)
        if code:
            sql += " AND code=?"
            params.append(code)
        sql += " ORDER BY ts DESC LIMIT 50"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [
            {
                "id": r[7], "ts": r[0], "action": r[1], "code": r[2], "name": r[3],
                "price": r[4], "qty": r[5], "note": r[6],
            }
            for r in rows
        ]
    except Exception:
        return []


def journal_list(session_id: str, days: int = 0, code: str = "") -> str:
    """查询日记,格式化输出(附带实时盈亏)。"""
    rows = journal_rows(session_id, days=days, code=code)
    if not rows:
        return "📭 没有找到日记记录。\n用 \"记一笔: 买入 茅台 100股\" 记录。"

    # 批量拉实时价算盈亏(失败不影响展示)
    cur_prices = {}
    codes = list({r["code"] for r in rows if r["price"] and r["price"] > 0})
    if codes:
        try:
            from data_fetcher import fetch_realtime

            for q in fetch_realtime(codes):
                cur_prices[q["code"]] = q.get("price", 0)
        except Exception:
            pass

    from datetime import datetime

    lines = []
    for i, r in enumerate(rows, 1):
        dt = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
        action_cn = ACTION_CN.get(r["action"], r["action"])
        line = f"{i}. [{dt}] {action_cn} {r['name'] or r['code']}({r['code']})"
        if r["price"] > 0:
            line += f" @{r['price']:g}"
            cur = cur_prices.get(r["code"], 0)
            if cur > 0:
                pnl = (cur / r["price"] - 1) * 100
                line += f" → 现价 {cur:g}({pnl:+.1f}%)"
        if r["qty"] > 0:
            line += f" x{r['qty']:g}股"
        if r["note"]:
            line += f" | {r['note']}"
        lines.append(line)
    header = f"📒 交易日记(最近 {len(rows)} 条"
    if days > 0:
        header += f",近 {days} 天"
    lines.insert(0, header + ")")
    return "\n".join(lines)
