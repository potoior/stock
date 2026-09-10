"""每周战报: 周日 18:00 聚合本周数据推送。

  python weekly_report.py            # 立即跑一次
  python weekly_report.py --dry-run  # 只打印不推送

内容:
- 大盘指数概览
- 群共享自选池本周涨跌
- 策略信号绩效(评估 + 胜率排行)
- 本周交易日记流水
"""

import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

import config_store
from journal import journal_rows

BASE = Path(__file__).parent
WATCHLIST_DB = BASE / "agent_watchlist.db"


def load_group_watchlist() -> list[dict]:
    """读群共享自选池(config.json chat_id 对应的 group key)。"""
    chat_id = config_store.get_section("feishu").get("chat_id", "")
    if not chat_id:
        return []
    group_key = f"group:{chat_id}"
    try:
        conn = sqlite3.connect(str(WATCHLIST_DB), timeout=10)
        rows = conn.execute(
            "SELECT code, name FROM watchlist WHERE session_id=? ORDER BY ts",
            (group_key,),
        ).fetchall()
        conn.close()
        return [{"code": r[0], "name": r[1]} for r in rows]
    except Exception:
        return []


def weekly_change(code: str) -> float | None:
    """近 5 个交易日涨跌幅(最新收盘 vs 5 日前收盘)。"""
    try:
        from data_fetcher import get_daily_data

        df = get_daily_data(code)
        if len(df) < 6:
            return None
        return float(df["close"].iloc[-1] / df["close"].iloc[-6] - 1)
    except Exception:
        return None


def build_index_lines() -> list[str]:
    """大盘指数概览。"""
    try:
        from stock_market_extras import fetch_index

        idx = fetch_index()
        if isinstance(idx, dict):
            idx = [idx]
        return [
            f"{it.get('name', '')} {it.get('price', 0):.2f} ({it.get('pct', 0):+.2f}%)"
            for it in idx
        ]
    except Exception:
        return ["(指数数据获取失败)"]


def build_watchlist_lines() -> list[str]:
    """自选池周涨跌(按周涨跌降序)。"""
    items = load_group_watchlist()
    if not items:
        return ["(自选池为空)"]
    rows = []
    for it in items[:15]:
        chg = weekly_change(it["code"])
        if chg is not None:
            rows.append((it["code"], it["name"], chg))
    if not rows:
        return ["(无数据)"]
    rows.sort(key=lambda x: -x[2])
    lines = []
    for code, name, chg in rows:
        arrow = "🔴" if chg < 0 else "🟢"
        lines.append(f"{arrow} {name or code}({code}) {chg:+.2%}")
    return lines


def build_journal_lines() -> list[str]:
    """本周交易日记(群会话)。"""
    chat_id = config_store.get_section("feishu").get("chat_id", "")
    if not chat_id:
        return ["(未配置 chat_id)"]
    from journal import ACTION_CN

    rows = journal_rows(chat_id, days=7)
    if not rows:
        return ["(本周无记录)"]
    lines = []
    for r in rows[:10]:
        dt = datetime.fromtimestamp(r["ts"]).strftime("%m-%d")
        action_cn = ACTION_CN.get(r["action"], r["action"])
        base = f"[{dt}] {action_cn} {r['name'] or r['code']}({r['code']})"
        if r["price"]:
            base += f" @{r['price']:g}"
        if r["note"]:
            base += f" | {r['note']}"
        lines.append(base)
    if len(rows) > 10:
        lines.append(f"...共 {len(rows)} 条")
    return lines


def build_card() -> dict:
    import signal_tracker

    now = datetime.now().strftime("%m-%d")
    idx_lines = build_index_lines()
    watch_lines = build_watchlist_lines()
    signal_tracker.evaluate_pending()
    sig_lines = signal_tracker.weekly_summary(weeks=1).splitlines()
    jr_lines = build_journal_lines()

    elements = []
    for title, lines in (
        (f"📈 大盘指数({now})", idx_lines),
        ("📌 自选池本周", watch_lines),
        ("📊 信号绩效", sig_lines),
        ("📒 本周日记", jr_lines),
    ):
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join([f"**{title}**", *lines])},
        })

    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📋 每周战报 {now}"},
            "template": "blue",
        },
        "elements": elements,
    }


def run_once(dry_run: bool = False):
    card = build_card()
    if dry_run:
        for el in card["elements"]:
            print(el["text"]["content"])
            print()
        return
    from feishu import FeishuBot

    bot = FeishuBot()
    if not bot.enabled:
        print("feishu: 未启用,跳过推送")
        return
    resp = bot.send_card(card)
    print("飞书推送" + ("成功" if resp and resp.get("code") == 0 else f"失败 {resp}"))


def main():
    ap = argparse.ArgumentParser(description="每周战报")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
