"""自选股新闻定时监控

每个交易日 11:45 / 20:30(systemd news-monitor.timer)抓取群共享自选池个股新闻,
只推送新消息到飞书群,已推送的自动去重(sqlite, 30 天过期)。

新增监控的股票首次不推送(只建立基线),避免一次刷屏历史新闻。

用法:
  python news_monitor.py             # 立即跑一次
  python news_monitor.py --dry-run   # 只打印不推送
"""

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from news_digest import fetch_stock_news

BASE = Path(__file__).parent
DB_PATH = BASE / "news_monitor.db"
WATCHLIST_DB = BASE / "agent_watchlist.db"
CONFIG_PATH = BASE / "config.json"

# 标题关键词分类
POSITIVE_KW = ["增持", "回购", "中标", "预增", "扭亏", "突破", "创新高", "涨停", "签约", "订单", "利好"]
NEGATIVE_KW = ["减持", "立案", "停牌", "预亏", "终止", "质押", "违约", "退市", "亏损", "处罚", "警示", "诉讼"]

PER_STOCK_LIMIT = 5  # 每只股票最多推送条数
TOTAL_LIMIT = 15  # 单次推送总条数上限
MAX_AGE_HOURS = 24  # 只推送最近 N 小时内的新闻


def _hash_url(url: str) -> str:
    return hashlib.md5(url.encode("utf-8")).hexdigest()


def _conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen (
        url_hash TEXT PRIMARY KEY, code TEXT, ts INTEGER)"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen(ts)")
    conn.commit()
    return conn


def classify(title: str) -> str:
    """按标题关键词标注利好/利空。"""
    if any(k in title for k in NEGATIVE_KW):
        return "🔴"
    if any(k in title for k in POSITIVE_KW):
        return "🟢"
    return "📝"


def within_hours(time_str: str, hours: int = MAX_AGE_HOURS) -> bool:
    """新闻时间是否在最近 N 小时内。无法解析时保守返回 True。"""
    try:
        t = datetime.strptime(time_str[:16], "%Y-%m-%d %H:%M")
        return (datetime.now() - t).total_seconds() <= hours * 3600
    except Exception:
        return True


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


def filter_new(code: str, items: list[dict]) -> list[dict]:
    """返回未推送过的新闻(按 url/title 哈希去重),不写入。"""
    conn = _conn()
    out = []
    for it in items:
        url = it.get("url") or it.get("title") or ""
        h = _hash_url(url)
        if conn.execute("SELECT 1 FROM seen WHERE url_hash=?", (h,)).fetchone():
            continue
        out.append(it)
    conn.close()
    return out


def mark_seen(code: str, items: list[dict]):
    conn = _conn()
    now = int(time.time())
    for it in items:
        url = it.get("url") or it.get("title") or ""
        conn.execute(
            "INSERT OR IGNORE INTO seen(url_hash, code, ts) VALUES(?,?,?)",
            (_hash_url(url), code, now),
        )
    # 清理 30 天前的记录
    conn.execute("DELETE FROM seen WHERE ts < ?", (now - 30 * 86400,))
    conn.commit()
    conn.close()


def has_seen_any(code: str) -> bool:
    """该股票是否已有推送基线(用于首次不推送)。"""
    conn = _conn()
    row = conn.execute("SELECT 1 FROM seen WHERE code=? LIMIT 1", (code,)).fetchone()
    conn.close()
    return bool(row)


def build_news_card(groups: list[dict], now=None) -> dict:
    """构造新闻推送卡片。groups: [{code, name, news: [{title,time,url,summary}]}]"""
    if now is None:
        now = datetime.now()
    lines = []
    for g in groups:
        lines.append(f"**{g['name'] or g['code']}**")
        for it in g["news"]:
            lines.append(f"{classify(it.get('title',''))} {it.get('time','')} {it.get('title','')}")
        lines.append("")
    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📢 自选股新闻速递 {now.strftime('%H:%M')}"},
            "template": "turquoise",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
        ],
    }


def run_once(dry_run: bool = False):
    watchlist = load_group_watchlist()
    if not watchlist:
        print(f"[{datetime.now():%H:%M:%S}] 群共享自选池为空(或未配置 chat_id),跳过")
        return

    print(f"[{datetime.now():%H:%M:%S}] 监控 {len(watchlist)} 只: {','.join(w['code'] for w in watchlist)}")
    groups = []
    for w in watchlist:
        code, name = w["code"], w["name"]
        try:
            items = fetch_stock_news(code, num=10)
        except Exception as e:
            print(f"  {code} {name} 新闻抓取失败: {e}")
            continue
        fresh = [it for it in items if within_hours(it.get("time", ""))]
        new_items = filter_new(code, fresh)
        if not new_items:
            continue
        if not has_seen_any(code):
            # 首次监控:只建基线不推送,避免历史新闻刷屏
            print(f"  {code} {name} 首次监控,记录 {len(new_items)} 条基线(不推送)")
            mark_seen(code, new_items)
            continue
        capped = new_items[:PER_STOCK_LIMIT]
        groups.append({"code": code, "name": name, "news": capped, "total": len(new_items)})
        # 未展示的也标记已读,避免下次重复计入
        mark_seen(code, new_items)

    # 总量限制(从最后一组往前裁)
    total = sum(len(g["news"]) for g in groups)
    while total > TOTAL_LIMIT and groups:
        g = groups[-1]
        g["news"].pop()
        total -= 1
        if not g["news"]:
            groups.remove(g)

    if not groups:
        print("无新消息")
        return

    card = build_news_card(groups)
    print(f"新消息 {total} 条:")
    for g in groups:
        for it in g["news"]:
            print(f"  {classify(it.get('title',''))} [{g['name']}] {it.get('title')}")
    if dry_run:
        print("(dry-run, 不推送)")
        return

    from feishu import FeishuBot

    bot = FeishuBot()
    if not bot.enabled:
        print("feishu 未启用,跳过推送")
        return
    resp = bot.send_card(card)
    print("飞书推送" + ("成功" if resp and resp.get("code") == 0 else f"失败 {resp}"))


def main():
    ap = argparse.ArgumentParser(description="自选股新闻定时监控")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()
    run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
