"""每日 9:00 全市场扫描 + 新闻综合日报

流程（每个交易日 09:00）：
  1. 爬取全 A 股（≈5500 只）实时行情，统计涨跌分布 / 涨停跌停 / 板块热度
  2. 抓取当日财经新闻（新浪 + 东财双源，复用 news_digest）
  3. 对成交额 / 涨幅 top 候选跑策略信号（strategy_engine.analyze）
  4. SensNews 综合分析「市场全景 + 候选信号 + 新闻」生成日报
  5. 写 reports/daily_YYYYMMDD.md

用法：
  python daily_scan.py                       # 立即跑一次
  python daily_scan.py --schedule "09:00"    # 纯 Python 定时，每天 09:00 自动跑
  python daily_scan.py --limit 1000          # 仅抓前1000只（调试加速）
  python daily_scan.py --top 5               # 对 top 5 候选跑策略信号
"""

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from news_digest import fetch_news

REPORTS = Path(__file__).parent / "reports"
HQ_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def fetch_market_page(page=1, num=100, sort="amount", asc=0):
    url = f"{HQ_URL}?page={page}&num={num}&sort={sort}&asc={asc}&node=hs_a&symbol=&_s_r_a=page"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.sina.com.cn/", "User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk")
    return json.loads(raw) or []


def norm_code(symbol):
    return symbol[2:] if symbol[:2].lower() in ("sh", "sz", "bj") else symbol


def fetch_market_all(limit=0):
    """抓取全市场 A 股实时行情，返回标准化字典列表。"""
    rows = []
    page = 1
    while True:
        try:
            batch = fetch_market_page(page=page, num=100, sort="amount", asc=0)
        except Exception:
            break
        if not batch:
            break
        rows.extend(batch)
        if limit and len(rows) >= limit:
            rows = rows[:limit]
            break
        if len(batch) < 100:
            break
        page += 1
    for r in rows:
        r["code6"] = norm_code(r.get("symbol", ""))[-6:].zfill(6)
    return rows


def market_stats(rows):
    """全市场涨跌分布统计。"""
    up = dn = flat = 0
    limit_up = limit_dn = 0
    total_amount = 0.0
    for r in rows:
        pct = float(r.get("changepercent") or 0)
        amt = float(r.get("amount") or 0)
        total_amount += amt
        if pct > 0:
            up += 1
        elif pct < 0:
            dn += 1
        else:
            flat += 1
        code = r.get("code6", "")
        is_kcb_cyb = code.startswith(("30", "68"))
        up_lim = 19.9 if is_kcb_cyb else 9.9
        if pct >= up_lim:
            limit_up += 1
        elif pct <= -up_lim:
            limit_dn += 1
    return {
        "total": len(rows),
        "up": up,
        "down": dn,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_dn,
        "total_amount_yi": round(total_amount / 1e8, 0),
    }


def top_candidates(rows, n=10):
    """按成交额排序取 top 候选。"""
    cands = []
    for r in rows:
        amt = float(r.get("amount") or 0)
        if amt < 1e8:  # 成交额至少 1 亿
            continue
        cands.append(
            {
                "code": r.get("code6", ""),
                "name": r.get("name", ""),
                "price": r.get("trade"),
                "pct": round(float(r.get("changepercent") or 0), 2),
                "amount_yi": round(amt / 1e8, 2),
                "turnover": round(float(r.get("turnoverratio") or 0), 2),
            }
        )
    cands.sort(key=lambda x: -x["amount_yi"])
    return cands[:n]


def analyze_candidates(cands, top_n=5):
    """对 top 候选跑完整策略信号。"""
    from strategy_engine import analyze

    out = []
    for c in cands[:top_n]:
        try:
            res = analyze(c["code"], use_ai=False)
            if "error" in res:
                out.append({**c, "verdict": "-", "summary": "-", "buy": [], "sell": []})
                continue
            out.append(
                {
                    **c,
                    "verdict": res["verdict"],
                    "summary": f"买{res['summary']['buy']}/卖{res['summary']['sell']}/观{res['summary']['hold']}",
                    "buy": [b["name"] + ":" + b["reason"] for b in res["buy_reasons"][:3]],
                    "sell": [s["name"] + ":" + s["reason"] for s in res["sell_reasons"][:3]],
                }
            )
        except Exception as e:
            out.append({**c, "verdict": "-", "summary": f"出错 {e}", "buy": [], "sell": []})
    return out


def build_prompt(stats, cands, news, date_str):
    """综合 prompt：市场全景 + 候选信号 + 新闻。"""
    lines_c = []
    for c in cands:
        sig = f"{c['verdict']}（{c['summary']}）"
        buys = "；".join(c["buy"][:2]) if c["buy"] else "无"
        lines_c.append(
            f"- {c['code']} {c['name']} 现价{c['price']} 涨{c['pct']:+.2f}% "
            f"成交{c['amount_yi']}亿 换手{c['turnover']}% → {sig}；买方信号：{buys}"
        )
    lines_n = []
    for i, n in enumerate(news[:25], 1):
        lines_n.append(f"{i}. [{n['time']} {n['source']}] {n['summary'] or n['title']}")
    return f"""你是 A 股量化分析师，请基于下面 {date_str} 开盘扫描数据与新闻，输出一份结构化日报（Markdown 中文）。

## 一、全市场扫描
- 总数 {stats["total"]} 只：上涨 {stats["up"]}，下跌 {stats["down"]}，平 {stats["flat"]}
- 涨停 {stats["limit_up"]} 只，跌停 {stats["limit_down"]} 只
- 全市场成交额约 {stats["total_amount_yi"]:.0f} 亿

## 二、成交额 top 候选与策略信号
{chr(10).join(lines_c) if lines_c else "（无候选）"}

## 三、当日新闻快讯（{len(news)} 条）
{chr(10).join(lines_n) if lines_n else "（无新闻）"}

请按以下结构输出：
1. **市场情绪总览**：结合涨跌停分布与新闻，判断今日偏多/偏空/中性，2-3 句
2. **热点板块/主线**：从候选股与新闻中提炼事件驱动板块
3. **重点候选解读**：挑 2-3 只 top 候选，结合其策略信号与新闻说明可操作性
4. **风险提示**：利空与不确定因素
5. **今日操作参考**：方向性判断（非投资建议）

严格基于给出数据，不要编造。"""


def run_once(limit, top, news_limit):
    now = datetime.now()
    print(f"== 全市场扫描 {now:%Y-%m-%d %H:%M} ==")
    rows = fetch_market_all(limit=limit)
    if not rows:
        print("行情抓取失败")
        return
    stats = market_stats(rows)
    print(
        f"抓取 {stats['total']} 只: 涨{stats['up']}/跌{stats['down']}/平{stats['flat']} "
        f"涨停{stats['limit_up']}/跌停{stats['limit_down']} 成交{stats['total_amount_yi']:.0f}亿"
    )

    cands = top_candidates(rows, n=max(top, 10))
    print(f"成交额 top 候选 {len(cands)} 只，对前 {top} 只跑策略信号...")
    analyzed = analyze_candidates(cands, top_n=top)

    print("抓取当日新闻...")
    news = fetch_news(news_limit)
    print(f"新闻 {len(news)} 条")

    print("AI 综合分析...")
    import re

    from ai_decider import AIDecider

    decider = AIDecider()
    body = decider.generate(build_prompt(stats, analyzed, news, now.strftime("%Y-%m-%d")), timeout=180)
    if body.startswith(("API限流", "API错误", "调用失败")):
        print("AI 调用失败:", body)
        body = f"（AI 调用失败：{body}）"
    else:
        body = re.split(r"\n\s*(?:Thinking\s*Process|推理过程)[:：]", body)[0].strip()

    save_report(stats, analyzed, news, body, now)
    print(f"\n日报已生成: {REPORTS / ('daily_' + now.strftime('%Y%m%d') + '.md')}")
    print("\n" + body[:1500])


def save_report(stats, cands, news, body, now):
    REPORTS.mkdir(exist_ok=True)
    fp = REPORTS / f"daily_{now.strftime('%Y%m%d')}.md"
    lines = [
        f"# 每日开盘扫描日报 {now:%Y-%m-%d %H:%M}",
        "\n## 一、全市场扫描\n",
        f"- 总数 **{stats['total']}** 只：上涨 {stats['up']} / 下跌 {stats['down']} / 平 {stats['flat']}",
        f"- 涨停 **{stats['limit_up']}** 只，跌停 **{stats['limit_down']}** 只",
        f"- 全市场成交额约 **{stats['total_amount_yi']:.0f} 亿**\n",
        "\n## 二、成交额 top 候选与策略信号\n",
        "| 代码 | 名称 | 现价 | 涨跌% | 成交(亿) | 换手% | 信号 |",
        "|------|------|------|-------|----------|-------|------|",
    ]
    for c in cands:
        lines.append(
            f"| {c['code']} | {c['name']} | {c['price']} | {c['pct']:+.2f} | "
            f"{c['amount_yi']} | {c['turnover']} | {c['verdict']}（{c['summary']}） |"
        )
    if cands:
        lines.append("\n**买方信号明细（top 候选）**")
        for c in cands[:5]:
            if c["buy"]:
                lines.append(f"- {c['name']}：{'；'.join(c['buy'])}")
    lines.append(f"\n## 三、当日新闻快讯（{len(news)} 条）\n")
    for i, n in enumerate(news[:30], 1):
        lines.append(f"{i}. **{n['title']}** `[{n['time']} {n['source']}]`")
    lines.append("\n---\n\n## 四、AI 综合分析\n")
    lines.append(body)
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="仅抓前N只(调试)")
    ap.add_argument("--top", type=int, default=5, help="对top N候选跑策略信号")
    ap.add_argument("--news-limit", type=int, default=20, help="每源新闻条数")
    ap.add_argument("--schedule", default="", metavar="HH:MM", help="纯Python定时,每天到点自动跑")
    args = ap.parse_args()

    if args.schedule:
        hh, mm = args.schedule.split(":")
        target = int(hh) * 3600 + int(mm) * 60
        print(f"已设定时, 每个交易日 {args.schedule} 自动生成日报（Ctrl+C 退出）")
        while True:
            now = datetime.now()
            secs = now.hour * 3600 + now.minute * 60 + now.second
            if now.weekday() < 5 and target <= secs < target + 600:
                try:
                    run_once(args.limit, args.top, args.news_limit)
                except Exception as e:
                    print("运行出错:", e)
                time.sleep(86400)
            else:
                delta = (target - secs) % 86400
                if delta <= 0:
                    delta += 86400
                print(f"{now.strftime('%H:%M:%S')} 等待 {delta // 3600}h {(delta % 3600) // 60}m 后运行")
                time.sleep(delta)
    else:
        run_once(args.limit, args.top, args.news_limit)


if __name__ == "__main__":
    main()
