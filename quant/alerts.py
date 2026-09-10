"""到价提醒: 盘中价格监控 + 飞书推送。

每个交易日盘中(09:30-11:30, 13:00-15:00)由 systemd price-alert.timer
每 5 分钟触发一次 oneshot:
- 批量拉取所有 active 提醒的实时价(新浪,一次全查)
- 命中目标价 -> 推送到创建提醒的会话(群/私聊) -> 标记 triggered(只触发一次)

用法:
  python alerts.py            # 立即检查一次(自动判断交易日/盘中)
  python alerts.py --dry-run  # 只打印不推送
  python alerts.py --force    # 跳过交易日/盘中时间判断(测试用)
"""

import argparse
import sqlite3
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
ALERTS_DB = BASE / "alerts.db"

# 盘中交易时段(触发检查的窗口)
SESSIONS = [((9, 30), (11, 30)), ((13, 0), (15, 0))]


def _db():
    """提醒 sqlite。启用 WAL 防多进程并发锁竞争(timer 与 bot 同时写)。"""
    conn = sqlite3.connect(str(ALERTS_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            code TEXT,
            name TEXT,
            op TEXT,
            target REAL,
            status TEXT,
            created_ts INTEGER,
            triggered_ts INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status)")
    conn.commit()
    return conn


def alert_add(session_id: str, code: str, name: str, op: str, target: float) -> str:
    """添加到价提醒。op: 'above'=涨到 / 'below'=跌破。"""
    try:
        conn = _db()
        dup = conn.execute(
            "SELECT id FROM alerts WHERE session_id=? AND code=? AND op=? AND target=? AND status='active'",
            (session_id, code, op, float(target)),
        ).fetchone()
        if dup:
            conn.close()
            return f"⚠️ 已存在相同的提醒: {name or code} {'涨到' if op == 'above' else '跌破'} {target:g}"
        conn.execute(
            "INSERT INTO alerts(session_id, code, name, op, target, status, created_ts) VALUES(?,?,?,?,?, 'active', ?)",
            (session_id, code, name, op, float(target), int(time.time())),
        )
        conn.commit()
        conn.close()
        op_cn = "涨到" if op == "above" else "跌破"
        return f"✅ 已设置提醒: {name or code}({code}) {op_cn} {target:g} 时通知你"
    except Exception as e:
        return f"❌ 设置提醒失败: {e}"


def alert_remove(session_id: str, code: str) -> str:
    """删除该会话某只股票的所有有效提醒。"""
    try:
        conn = _db()
        cur = conn.execute(
            "DELETE FROM alerts WHERE session_id=? AND code=? AND status='active'",
            (session_id, code),
        )
        n = cur.rowcount
        conn.commit()
        conn.close()
        return f"✅ 已删除 {code} 的 {n} 条提醒" if n else f"⚠️ 没有找到 {code} 的有效提醒"
    except Exception as e:
        return f"❌ 删除提醒失败: {e}"


def alert_list(session_id: str) -> list[dict]:
    """列出有效提醒。"""
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT code, name, op, target, id FROM alerts "
            "WHERE session_id=? AND status='active' ORDER BY id",
            (session_id,),
        ).fetchall()
        conn.close()
        return [
            {"id": r[4], "code": r[0], "name": r[1], "op": r[2], "target": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def _chat_id_of(session_id: str) -> str:
    """session_id = '<chat_id>:<sender>' -> chat_id(提醒发到会话,不区分人)。"""
    return session_id.split(":", 1)[0] if ":" in session_id else session_id


def is_market_time(now=None) -> bool:
    """当前是否在盘中交易时段。"""
    if now is None:
        now = datetime.now()
    hm = (now.hour, now.minute)
    for start, end in SESSIONS:
        if (start[0], start[1]) <= hm <= (end[0], end[1]):
            return True
    return False


def check_alerts_once(dry_run: bool = False) -> list[str]:
    """检查所有 active 提醒,返回触发的消息列表(推送+标记)。"""
    from data_fetcher import fetch_realtime

    conn = _db()
    rows = conn.execute(
        "SELECT id, session_id, code, name, op, target FROM alerts WHERE status='active'"
    ).fetchall()
    if not rows:
        conn.close()
        return []

    by_code = {}
    for r in rows:
        by_code.setdefault(r[2], []).append(r)
    quotes = {q["code"]: q for q in fetch_realtime(list(by_code))}

    messages = []
    triggered_ids = []
    now = datetime.now().strftime("%m-%d %H:%M")
    for code, items in by_code.items():
        q = quotes.get(code)
        if not q or not q.get("price"):
            continue
        price = float(q["price"])
        for row_id, session_id, _, name, op, target in items:
            hit = price >= target if op == "above" else price <= target
            if not hit:
                continue
            op_cn = "涨到" if op == "above" else "跌破"
            pct = q.get("pct", 0)
            msg = (
                f"🔔 到价提醒 {name or code}({code})\n"
                f"{op_cn} {target:g} | 现价 {price:.2f}({pct:+.2f}%)\n"
                f"触发于 {now}"
            )
            messages.append((session_id, msg))
            triggered_ids.append(row_id)

    if triggered_ids and not dry_run:
        from feishu import FeishuBot

        bot = FeishuBot()
        for session_id, msg in messages:
            bot.send_text(msg, chat_id=_chat_id_of(session_id))
        conn.execute(
            f"UPDATE alerts SET status='triggered', triggered_ts=? "
            f"WHERE id IN ({','.join('?' * len(triggered_ids))})",
            [int(time.time()), *triggered_ids],
        )
        conn.commit()
    conn.close()
    return [m for _, m in messages]


def main():
    ap = argparse.ArgumentParser(description="到价提醒监控")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    ap.add_argument("--force", action="store_true", help="跳过交易日/盘中判断")
    args = ap.parse_args()

    if not args.force:
        try:
            from daily_scan import is_trading_day

            if not is_trading_day():
                return
        except ImportError:
            pass
        if not is_market_time():
            return

    msgs = check_alerts_once(dry_run=args.dry_run)
    ts = datetime.now().strftime("%H:%M:%S")
    if msgs:
        for m in msgs:
            print(f"[{ts}] 触发: {m.splitlines()[1]}")
    else:
        print(f"[{ts}] 无提醒触发")


if __name__ == "__main__":
    main()
