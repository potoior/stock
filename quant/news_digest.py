"""每日财经新闻订阅与AI分析日报

从新浪财经 + 东方财富 双源抓取当日财经新闻，去重合并后用 SensNews 大模型
生成一份「市场情绪 / 关注板块 / 热点事件 / 风险提示」日报，写到 reports/。

用法：
  python news_digest.py                     # 抓取当天新闻 + AI 生成日报
  python news_digest.py --no-ai             # 只抓新闻不调AI（快速调试）
  python news_digest.py --limit 20          # 每源最多抓20条
  python news_digest.py --schedule "08:30"  # 纯Python定时,到点自动运行(不依赖系统cron/systemd)

数据源：新浪财经滚动新闻(feed.mix.sina.com.cn) / 东方财富财经快讯(np-listapi.eastmoney.com)
"""

import argparse
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPORTS = Path(__file__).parent / "reports"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _http_get(url, referer):
    req = urllib.request.Request(url, headers={"Referer": referer, "User-Agent": UA})
    return urllib.request.urlopen(req, timeout=15).read()


def fetch_sina_news(num=30):
    """新浪财经滚动新闻，intime 为 epoch 秒。"""
    url = f"https://feed.mix.sina.com.cn/api/roll/get?rnd=1&pageid=153&lid=2509&k=&num={num}&page=1"
    raw = _http_get(url, "https://finance.sina.com.cn/").decode("utf-8", "replace")
    try:
        items = json.loads(raw)["result"]["data"]
    except Exception:
        return []
    out = []
    for it in items:
        ts = it.get("intime")
        try:
            dt = datetime.fromtimestamp(int(ts)) if ts else datetime.now()
        except Exception:
            dt = datetime.now()
        out.append(
            {
                "title": (it.get("title") or "").strip(),
                "summary": (it.get("summary") or "").strip()[:120],
                "time": dt.strftime("%Y-%m-%d %H:%M"),
                "source": "新浪财经",
                "url": it.get("url") or "",
            }
        )
    return out


def fetch_em_news(num=30):
    """东方财富财经快讯，showTime 形如 2026-08-17 16:44:06。"""
    url = (
        "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        f"?client=web&biz=web_news_col&column=350&order=1"
        f"&needInteractData=0&page_index=1&page_size={num}&req_trace=1"
    )
    raw = _http_get(url, "https://finance.eastmoney.com/").decode("utf-8", "replace")
    try:
        items = (json.loads(raw).get("data") or {}).get("list") or []
    except Exception:
        return []
    out = []
    for it in items:
        t = it.get("showTime") or it.get("art_time") or it.get("showtime") or ""
        if t and len(t) >= 16:
            t = t[:16]
        out.append(
            {
                "title": (it.get("title") or "").strip(),
                "summary": (it.get("summary") or "").strip()[:120],
                "time": t,
                "source": "东方财富",
                "url": it.get("url") or "",
            }
        )
    return out


def _norm_title(title):
    return "".join(title.split())


def fetch_news(num=30):
    """双源抓取合并去重（按标题归一化）。"""
    sina = fetch_sina_news(num)
    em = fetch_em_news(num)
    seen = set()
    merged = []
    for it in sina + em:
        key = _norm_title(it["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(it)
    return merged


def fetch_stock_news(code: str, num: int = 15, strict: bool = True) -> list[dict]:
    """抓取个股相关新闻(东财搜索接口,按股票名搜索)。

    Args:
        code: 6 位股票代码,如 301189
        num: 返回新闻条数,默认 15
        strict: True=只保留 title/summary 明确提到股票名或代码的新闻(过滤列表型无关新闻);
                False=返回所有搜索结果

    Returns: [{title, summary, time, source, url}, ...] 按时间降序,失败返回 []

    实现要点:
      - 先用 stock_names 解析股票名(失败时回退用代码搜)
      - 调东财搜索接口 search-api-web.eastmoney.com 搜 cmsArticleWebOld 类型
      - 清理 <em> 高亮标签
      - 截断 summary 到 120 字
    """
    if not code or not (isinstance(code, str) and code.isdigit() and len(code) == 6):
        return []

    # 1. 解析股票名(失败回退用代码)
    keyword = code
    try:
        # 反查:从缓存找 code -> name
        import sqlite3

        import stock_names as sn
        db = sn.DB_PATH
        conn = sqlite3.connect(str(db), timeout=5)
        rows = conn.execute(
            "SELECT name, full_name FROM stock_names WHERE code=?", (code,)
        ).fetchall()
        conn.close()
        if rows:
            for name, full_name in rows:
                if name:
                    keyword = name
                    break
                if full_name:
                    keyword = full_name
                    break
        else:
            # 缓存没有,主动 resolve 一次触发搜索 + 缓存
            resolved = sn.resolve_code(code)
            if resolved:
                # 再查一次缓存
                conn = sqlite3.connect(str(db), timeout=5)
                rows = conn.execute(
                    "SELECT name FROM stock_names WHERE code=?", (code,)
                ).fetchall()
                conn.close()
                if rows and rows[0][0]:
                    keyword = rows[0][0]
    except Exception:
        pass

    # 2. 调东财搜索接口
    import urllib.parse
    param = json.dumps({
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientVersion": "curr",
    })
    url = (
        "https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
        + urllib.parse.quote(param)
    )
    try:
        raw = _http_get(url, "https://so.eastmoney.com/").decode("utf-8", "replace")
    except Exception:
        return []

    # 3. 解析 JSONP("cb({...})") -> JSON
    body = raw.strip()
    if body.startswith("cb(") and body.endswith(")"):
        body = body[3:-1]
    try:
        data = json.loads(body)
    except Exception:
        return []

    items = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
    import re
    out = []
    stock_name = keyword if keyword != code else None
    for it in items:
        title = (it.get("title") or "").strip()
        title = re.sub(r"</?em>", "", title)
        summary = (it.get("content") or "").strip()
        summary = re.sub(r"</?em>", "", summary)[:120]
        # strict 过滤:title 或 summary 必须明确提到股票名或代码
        if strict:
            hit_name = stock_name and (stock_name in title or stock_name in summary)
            hit_code = code in title or code in summary
            if not (hit_name or hit_code):
                continue
        out.append({
            "title": title,
            "summary": summary,
            "time": (it.get("date") or "")[:16],
            "source": it.get("mediaName") or "东方财富",
            "url": it.get("url") or "",
        })
        if len(out) >= num:
            break
    return out


def build_prompt(news, date_str):
    lines = []
    for i, n in enumerate(news[:40], 1):
        summary = n["summary"] or n["title"]
        lines.append(f"{i}. [{n['time']} {n['source']}] {summary}")
    return f"""你是A股宏观/行业分析师，请基于下面的 {date_str} 当日财经新闻快讯，
输出一份结构清晰的日报（用 Markdown，中文）。

## 当日新闻快讯（{len(news)} 条）
{chr(10).join(lines) if lines else "（当日暂无新闻）"}

请按以下结构输出：
1. **市场情绪总览**：整体偏多/偏空/中性，用2-3句概括
2. **关注板块/主线**：列出事件驱动的热点板块及逻辑（如有）
3. **关键事件解读**：挑选2-3条最重要新闻，说明其对A股的影响
4. **风险提示**：值得警惕的利空或不确定因素
5. **操作参考**：给短线情绪面的方向性判断（非投资建议）

注意严格基于给出的新闻内容，不要编造数据。"""


def generate_report(news, use_ai=True):
    date_str = datetime.now().strftime("%Y-%m-%d")
    if not use_ai:
        return f"（--no-ai 模式，未调用模型。抓取到 {len(news)} 条当日新闻）"
    import re

    from ai_decider import AIDecider

    decider = AIDecider()
    prompt = build_prompt(news, date_str)
    body = decider.generate(prompt, timeout=120)
    if body.startswith(("API限流", "API错误", "调用失败")):
        return body
    # 去掉模型推理(thinking/reasoning)尾部，只保留正文
    body = re.split(r"\n\s*(?:Thinking\s*Process|推理过程)[:：]", body)[0].strip()
    return body


def save_report(news, body):
    REPORTS.mkdir(exist_ok=True)
    now = datetime.now()
    fname = now.strftime("news_%Y%m%d.md")
    fp = REPORTS / fname
    lines = [
        f"# 每日财经新闻分析日报 {now:%Y-%m-%d %H:%M}",
        f"\n## 今日新闻快讯（抓取 {len(news)} 条，双源合并去重）\n",
    ]
    for i, n in enumerate(news[:40], 1):
        lines.append(f"{i}. **{n['title']}**  `[{n['time']} {n['source']}]`")
    lines.append("\n---\n\n## AI 分析\n")
    lines.append(body)
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def run_once(limit, use_ai):
    print("== 抓取当日财经新闻 ==")
    news = fetch_news(limit)
    print(f"合并去重后 {len(news)} 条")
    if not news:
        print("当日无新闻")
        return
    for n in news[:10]:
        print(f"  - [{n['time']} {n['source']}] {n['title'][:40]}")
    print("\n== AI 生成日报 ==")
    body = generate_report(news, use_ai)
    fp = save_report(news, body)
    print(f"\n日报已保存: {fp}")
    print("\n" + body[:2000])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--schedule", default="", metavar="HH:MM", help="纯Python定时,每天到点自动运行一次")
    args = ap.parse_args()

    if args.schedule:
        hh, mm = args.schedule.split(":")
        target = int(hh) * 3600 + int(mm) * 60
        print(f"已设定时, 每个交易日 {args.schedule} 自动生成日报")
        while True:
            now = datetime.now()
            secs = now.hour * 3600 + now.minute * 60 + now.second
            weekday = now.weekday()
            if weekday < 5 and secs >= target and secs < target + 600:
                run_once(args.limit, not args.no_ai)
                # 运行完后等待到下一个目标时点
                sleep_secs = 86400 - ((secs - target) % 86400) + 60
            else:
                delta = (target - secs) % 86400
                if delta <= 0:
                    delta += 86400
                sleep_secs = delta
            print(
                f"下次运行: {datetime.now().strftime('%H:%M:%S')} 后等待 {sleep_secs // 3600}h {(sleep_secs % 3600) // 60}m"
            )
            time.sleep(sleep_secs)
    else:
        run_once(args.limit, not args.no_ai)


if __name__ == "__main__":
    main()
