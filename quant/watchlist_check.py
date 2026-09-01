"""持仓与自选股每日体检

每个交易日 15:30(systemd watchlist-check.timer)盘后推送:
- 持仓体检:群内所有持仓的实时盈亏 + 策略信号(重点标注卖出/逃顶信号)
- 自选信号:群共享自选池逐只策略分析,卖出信号优先

用法:
  python watchlist_check.py             # 立即跑一次
  python watchlist_check.py --dry-run   # 只打印不推送
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
WATCHLIST_DB = BASE / "agent_watchlist.db"
PORTFOLIO_DB = BASE / "portfolio.db"
CONFIG_PATH = BASE / "config.json"


def load_group_watchlist() -> list[dict]:
    """读群共享自选池(config.json chat_id 对应的 group key)。"""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        chat_id = (cfg.get("feishu") or {}).get("chat_id", "")
    except Exception:
        chat_id = ""
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


def load_chat_positions() -> list[dict]:
    """读本群所有用户的持仓(按 chat_id 前缀匹配)。"""
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        chat_id = (cfg.get("feishu") or {}).get("chat_id", "")
    except Exception:
        return []
    if not chat_id:
        return []
    try:
        conn = sqlite3.connect(str(PORTFOLIO_DB), timeout=10)
        rows = conn.execute(
            "SELECT code, name, qty, cost, buy_date FROM positions "
            "WHERE session_id LIKE ? ORDER BY ts",
            (f"{chat_id}:%",),
        ).fetchall()
        conn.close()
        return [
            {"code": r[0], "name": r[1], "qty": r[2], "cost": r[3], "buy_date": r[4]}
            for r in rows
        ]
    except Exception:
        return []


def analyze_one(code: str) -> dict | None:
    """单只股票策略分析,返回 {name, price, pct, verdict, buys, sells}。"""
    from strategy_engine import analyze

    try:
        r = analyze(code, use_ai=False)
    except Exception:
        return None
    if "error" in r:
        return None
    rt = r.get("realtime") or {}
    return {
        "name": rt.get("name", ""),
        "price": rt.get("price", 0),
        "pct": rt.get("pct", 0),
        "verdict": r.get("verdict", "-"),
        "buys": [f"{b.get('name','')}: {b.get('reason','')}" for b in r.get("buy_reasons", [])[:3]],
        "sells": [f"{s.get('name','')}: {s.get('reason','')}" for s in r.get("sell_reasons", [])[:3]],
    }


def build_card(positions: list[dict], watchlist: list[dict]) -> dict | None:
    """构造体检卡片。positions/watchlist 为原始行,内部做分析。"""
    # 去重后的分析对象:持仓优先
    codes = []
    for p in positions:
        if p["code"] not in codes:
            codes.append(p["code"])
    for w in watchlist:
        if w["code"] not in codes:
            codes.append(w["code"])
    if not codes:
        return None

    print(f"[{datetime.now():%H:%M:%S}] 分析 {len(codes)} 只: {','.join(codes)}")
    results = {}
    for code in codes:
        r = analyze_one(code)
        if r:
            results[code] = r
        print(f"  {code}: {'ok' if r else '失败'}")

    lines = []

    # ---- 持仓体检 ----
    if positions:
        lines.append("**💼 持仓体检**")
        total_cost = total_mv = 0.0
        has_px = False
        for p in positions:
            r = results.get(p["code"], {})
            px = r.get("price", 0)
            if px > 0:
                has_px = True
                mv = px * p["qty"]
                pnl = (px - p["cost"]) * p["qty"]
                pnl_pct = (px / p["cost"] - 1) * 100 if p["cost"] else 0
                total_cost += p["cost"] * p["qty"]
                total_mv += mv
                emoji = "🔴" if pnl < 0 else "🟢"
                lines.append(
                    f"{emoji} **{p['name'] or r.get('name','')}({p['code']})** "
                    f"{p['qty']:g}股 成本{p['cost']:.2f} → {px:.2f} "
                    f"{pnl:+.0f}元({pnl_pct:+.1f}%) [{r.get('verdict','-')}]"
                )
            else:
                lines.append(
                    f"⚪ **{p['name']}({p['code']})** {p['qty']:g}股 成本{p['cost']:.2f}(无实时价)"
                )
            for s in r.get("sells", []):
                lines.append(f"  ⚠️ {s}")
        if has_px and total_cost > 0:
            lines.append(
                f"合计: 成本 {total_cost:.0f} → 市值 {total_mv:.0f},"
                f"浮动盈亏 {total_mv - total_cost:+.0f} 元"
            )
        lines.append("")

    # ---- 自选信号 ----
    if watchlist:
        lines.append("**📌 自选信号**")
        for w in watchlist:
            r = results.get(w["code"])
            if not r:
                continue
            pct = r["pct"]
            arrow = "↑" if pct > 0 else "↓" if pct < 0 else "-"
            lines.append(
                f"{arrow} **{w['name'] or r.get('name','')}({w['code']})** "
                f"{r['price']:.2f}({pct:+.2f}%) [{r['verdict']}]"
            )
            # 卖出信号优先展示
            for s in r.get("sells", []):
                lines.append(f"  ⚠️ {s}")
        lines.append("")

    if not positions and not watchlist:
        return None

    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📋 持仓与自选体检 {datetime.now().strftime('%m-%d %H:%M')}"},
            "template": "orange",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        ],
    }


def run_once(dry_run: bool = False):
    positions = load_chat_positions()
    watchlist = load_group_watchlist()
    if not positions and not watchlist:
        print(f"[{datetime.now():%H:%M:%S}] 无持仓且自选池为空,跳过")
        return

    card = build_card(positions, watchlist)
    if not card:
        print("无数据可推送")
        return
    if dry_run:
        print("--- 卡片内容(dry-run) ---")
        print(card["elements"][0]["text"]["content"])
        return

    from feishu import FeishuBot

    bot = FeishuBot()
    if not bot.enabled:
        print("feishu 未启用,跳过推送")
        return
    resp = bot.send_card(card)
    print("飞书推送" + ("成功" if resp and resp.get("code") == 0 else f"失败 {resp}"))


def main():
    ap = argparse.ArgumentParser(description="持仓与自选股每日体检")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
