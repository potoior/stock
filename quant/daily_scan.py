"""每日 9:25 全市场扫描 + 玉姐精选 + 新闻综合日报

流程（每个交易日 09:25,开盘后 5 分钟数据稳定）：
  1. 爬取全 A 股（≈5500 只）实时行情，统计涨跌分布 / 涨停跌停 / 板块热度
  2. 调用 yujie_scan.run_once() 跑玉姐精选全市场打分（数据共用 stock_cache.db）
  3. 取玉姐 Top N 作为候选，附加 strategy_engine.analyze 完整策略信号
  4. 抓取当日财经新闻（新浪 + 东财双源，复用 news_digest）
  5. SensNews 综合分析「市场全景 + 玉姐候选 + 新闻」生成日报
  6. 写 reports/daily_YYYYMMDD.md

用法：
  python daily_scan.py                       # 立即跑一次
  python daily_scan.py --schedule "09:25"    # 纯 Python 定时，每天 09:25 自动跑
  python daily_scan.py --limit 1000          # 仅抓前1000只（调试加速）
  python daily_scan.py --top 10              # 对玉姐 Top 10 跑策略信号
"""

import argparse
import json
import logging
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from news_digest import fetch_news

REPORTS = Path(__file__).parent / "reports"
HQ_URL = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
log = logging.getLogger("quant.daily")

# A 股法定节假日(每年 ~20 天,手动维护,年初更新一次)
# 格式: YYYYMMDD,只列休市日(不含调休上班的周末)
HOLIDAYS_2026 = {
    # 元旦
    "20260101",
    # 春节(除夕~初六,2026 春节 2 月 17 日)
    "20260216", "20260217", "20260218", "20260219", "20260220", "20260221", "20260222",
    # 清明
    "20260404", "20260405", "20260406",
    # 劳动节
    "20260501", "20260502", "20260503", "20260504", "20260505",
    # 端午
    "20260619", "20260620", "20260621",
    # 中秋(2026 中秋 9 月 25 日,与国庆相邻)
    "20260925", "20260926", "20260927",
    # 国庆
    "20261001", "20261002", "20261003", "20261004", "20261005", "20261006", "20261007",
}
HOLIDAYS = set(HOLIDAYS_2026)  # 后续年份可加 HOLIDAYS_2027 等


def is_trading_day(now=None) -> bool:
    """判断当前是否为 A 股交易日(周一~周五 且 非节假日)。"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    today = now.strftime("%Y%m%d")
    # 检查节假日表是否覆盖当前年份(避免跨年时未更新导致误判)
    if str(now.year) not in {s[:4] for s in HOLIDAYS}:
        log.warning("HOLIDAYS 未覆盖 %d 年,请更新 daily_scan.py 的 HOLIDAYS_%d", now.year, now.year)
    return today not in HOLIDAYS


def fetch_market_page(page=1, num=100, sort="amount", asc=0):
    url = f"{HQ_URL}?page={page}&num={num}&sort={sort}&asc={asc}&node=hs_a&symbol=&_s_r_a=page"
    req = urllib.request.Request(url, headers={"Referer": "http://finance.sina.com.cn/", "User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk")
    return json.loads(raw) or []


def norm_code(symbol):
    return symbol[2:] if symbol[:2].lower() in ("sh", "sz", "bj") else symbol


def fetch_market_all(limit=0, max_pages=80):
    """抓取全市场 A 股实时行情,返回标准化字典列表。

    每页失败重试 3 次(指数退避 1s/2s/4s),应对开盘瞬间 HTTP 456 限流。
    仍失败才跳过该页继续下一页,避免单页网络抖动丢掉后面所有页。
    """
    rows = []
    page = 1
    while page <= max_pages:
        batch = None
        last_err = None
        for attempt in range(3):  # 指数退避 1s/2s/4s
            try:
                batch = fetch_market_page(page=page, num=100, sort="amount", asc=0)
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1, 2 秒
        if batch is None:
            log.warning("fetch_market_page page=%d 重试 3 次仍失败: %s,跳过", page, last_err)
            page += 1
            continue
        if not batch:
            break  # 真正的尾页(空数据)才退出
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


def top_candidates_from_yujie(top_n=10):
    """从玉姐精选榜单取 Top N 作为 AI 日报候选。
    复用 yujie_scan 已扫描结果,避免重复抓数据。
    """
    import yujie_scan

    picks = yujie_scan.load_picks()
    out = []
    for p in picks[:top_n]:
        d = p.get("detail") or {}
        out.append(
            {
                "code": p["code"],
                "name": p["name"],
                "price": d.get("price"),
                "pct": 0.0,  # 玉姐扫描不存实时涨跌幅，日报中保留字段
                "amount_yi": 0.0,  # 同上
                "turnover": 0.0,
                "score": p["score"],
                "hits": p["hits"],
                "rank": p["rank"],
            }
        )
    return out


def scan_hotspot_stocks(top_sectors=5, top_stocks_per_sector=5):
    """市场热点选股(操练大全14章 hotspot_select):按板块涨幅排名取成分股。

    实现思路:
      1. 调用东财板块接口拉概念+行业板块涨跌幅排名(fid=f3 涨跌幅降序)
      2. 取 top_sectors 个最强板块
      3. 每个板块按成交额降序取 top_stocks_per_sector 只成分股
      4. 返回 [{sector, sector_code, code, ...}, ...]

    失败时返回空列表(不影响主流程)。
    """
    out = []
    try:
        # 1. 拉板块涨幅排名(概念 t=2 + 行业 t=3)
        sectors = []
        for t in (2, 3):
            url = (
                f"http://17.push2.eastmoney.com/api/qt/clist/get"
                f"?pn=1&pz=20&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:{t}"
                f"&fields=f12,f14,f3"
            )
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
            for r in (data.get("data") or {}).get("diff", []) or []:
                sectors.append({
                    "code": r.get("f12", ""),
                    "name": r.get("f14", ""),
                    "pct": r.get("f3", 0),
                })
        sectors.sort(key=lambda x: float(x.get("pct", 0) or 0), reverse=True)
        # 2. 取 top_sectors 个最强板块,每个拉 top_stocks_per_sector 成分股
        for s in sectors[:top_sectors]:
            bk = s["code"]
            if not bk:
                continue
            members_url = (
                f"http://17.push2.eastmoney.com/api/qt/clist/get"
                f"?pn=1&pz={top_stocks_per_sector}&po=1&np=1&fltt=2&invt=2&fid=f6"
                f"&fs=b:{bk}&fields=f12,f14,f3,f6"
            )
            try:
                req = urllib.request.Request(members_url, headers={"User-Agent": UA})
                data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
                for m in (data.get("data") or {}).get("diff", []) or []:
                    code = m.get("f12", "")
                    if not (code and isinstance(code, str) and code.isdigit() and len(code) == 6):
                        continue
                    out.append({
                        "sector": s["name"],
                        "sector_code": bk,
                        "code": code,
                        "name": m.get("f14", ""),
                        "pct": m.get("f3", 0),
                        "amount_yi": round((m.get("f6", 0) or 0) / 1e8, 2),
                    })
            except Exception:
                continue
    except Exception:
        return []
    return out


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
    """综合 prompt：市场全景 + 玉姐候选 + 新闻。"""
    lines_c = []
    for c in cands:
        sig = f"{c['verdict']}（{c['summary']}）"
        buys = "；".join(c["buy"][:2]) if c["buy"] else "无"
        hits = "、".join(c.get("hits", [])[:3]) if c.get("hits") else "-"
        lines_c.append(
            f"- 第{c['rank']}名 {c['code']} {c['name']} 玉姐评分 {c['score']} 分 "
            f"命中规则：{hits} → 策略信号 {sig}；买方理由：{buys}"
        )
    lines_n = []
    for i, n in enumerate(news[:25], 1):
        lines_n.append(f"{i}. [{n['time']} {n['source']}] {n['summary'] or n['title']}")
    return f"""你是 A 股量化分析师，请基于下面 {date_str} 开盘扫描数据与新闻，输出一份结构化日报（Markdown 中文）。

## 一、全市场扫描
- 总数 {stats["total"]} 只：上涨 {stats["up"]}，下跌 {stats["down"]}，平 {stats["flat"]}
- 涨停 {stats["limit_up"]} 只，跌停 {stats["limit_down"]} 只
- 全市场成交额约 {stats["total_amount_yi"]:.0f} 亿{"（注：当前为集合竞价时段，成交额仅含竞价量，个股涨跌幅为开盘缺口）" if stats.get("premarket") else ""}

## 二、玉姐精选 Top 候选（按评分排序，含策略信号）
{chr(10).join(lines_c) if lines_c else "（无候选）"}

## 三、当日新闻快讯（{len(news)} 条）
{chr(10).join(lines_n) if lines_n else "（无新闻）"}

请按以下结构输出：
1. **市场情绪总览**：结合涨跌停分布与新闻，判断今日偏多/偏空/中性，2-3 句
2. **热点板块/主线**：从玉姐候选股与新闻中提炼事件驱动板块
3. **重点候选解读**：挑 2-3 只 Top 候选，结合其玉姐评分、命中规则、策略信号与新闻说明可操作性
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
    # 盘前(9:30 前)运行:成交额仅含集合竞价量,标注给 AI 正确解读
    stats["premarket"] = now.hour < 9 or (now.hour == 9 and now.minute < 30)
    print(
        f"抓取 {stats['total']} 只: 涨{stats['up']}/跌{stats['down']}/平{stats['flat']} "
        f"涨停{stats['limit_up']}/跌停{stats['limit_down']} 成交{stats['total_amount_yi']:.0f}亿"
    )

    # 跑玉姐精选全市场扫描（数据共用 stock_cache.db，避免重复抓取）
    print("启动玉姐精选全市场扫描...")
    import yujie_scan

    yujie_scan.run_once(limit=0)
    cands = top_candidates_from_yujie(top_n=max(top, 10))
    print(f"玉姐精选 Top {len(cands)} 候选，对前 {top} 只跑完整策略信号...")
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

    # 飞书推送日报卡片(失败仅打日志,不影响主流程)
    try:
        from feishu import send_daily_to_feishu

        ok = send_daily_to_feishu(stats, analyzed, body, now=now)
        if ok:
            print("飞书推送成功")
    except Exception as e:
        print(f"飞书推送失败: {e}")

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
        "\n## 二、玉姐精选 Top 候选与策略信号\n",
        "| 排名 | 代码 | 名称 | 玉姐评分 | 命中规则 | 策略信号 |",
        "|------|------|------|----------|----------|----------|",
    ]
    for c in cands:
        hits = "、".join(c.get("hits", [])[:3]) if c.get("hits") else "-"
        lines.append(
            f"| {c['rank']} | {c['code']} | {c['name']} | {c['score']} | {hits} | "
            f"{c['verdict']}（{c['summary']}） |"
        )
    if cands:
        lines.append("\n**买方信号明细（top 候选）**")
        for c in cands[:5]:
            if c["buy"]:
                lines.append(f"- {c['name']}（玉姐 {c['score']}分）：{'；'.join(c['buy'])}")
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
            if is_trading_day(now) and target <= secs < target + 600:
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
