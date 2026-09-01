"""新闻驱动选股:多段因果推理引擎

流程(ReAct 式接地推理,非一次性幻觉):
  1. 抓取当日新闻
  2. LLM 第一段推理:筛选可交易事件 + 受影响概念板块
  3. 数据接地:概念名 → 东财概念板块 → 真实成分股 + 当日实际数据
  4. LLM 第二段推理:事件 → 传导链 → 个股,输出因果推理与推荐理由

用法:
  python news_reasoning.py             # 跑一次并推送飞书
  python news_reasoning.py --dry-run   # 只打印不推送
"""

import argparse
import json
import logging
import urllib.request
from datetime import datetime

log = logging.getLogger("quant")

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


def _get_json(url, referer, timeout=10, retries=3):
    """带重试的 JSON 抓取(东财偶发拒连,指数退避 1s/2s/4s)。"""
    import time as _t

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Referer": referer}
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
            return json.loads(raw)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                _t.sleep(2 ** attempt)
    raise last_err


# ---------------- 数据接地:概念板块 → 成分股 ----------------


def fetch_board_list(board_type="concept"):
    """拉取东财概念/行业板块列表(翻页),返回 {板块名: BK代码}。

    pz>200 会被接口拒连(Remote end closed),用 pz=100 翻 4 页凑全量。
    """
    fs = "m:90+t:3" if board_type == "concept" else "m:90+t:2"
    out = {}
    for pn in range(1, 5):
        url = (
            f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
            f"&fltt=2&invt=2&fid=f3&fs={fs}&fields=f12,f14"
        )
        try:
            data = _get_json(url, "https://data.eastmoney.com/")
        except Exception as e:
            log.warning("fetch_board_list 第 %d 页失败: %s", pn, e)
            break
        rows = (data.get("data") or {}).get("diff") or []
        if not rows:
            break
        for r in rows:
            if r.get("f12"):
                out[r.get("f14", "")] = r["f12"]
        if len(rows) < 100:
            break
    return out


def fetch_board_members(bk_code: str, top_n: int = 5) -> list[dict]:
    """板块成分股,按主力净流入降序,返回 [{code, name, pct, main_net}]。"""
    url = (
        f"https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={top_n}&po=1&np=1"
        f"&fltt=2&invt=2&fid=f62&fs=b:{bk_code}&fields=f12,f14,f2,f3,f62"
    )
    try:
        data = _get_json(url, "https://data.eastmoney.com/")
        rows = (data.get("data") or {}).get("diff") or []
        out = []
        for r in rows:
            out.append({
                "code": r.get("f12", ""),
                "name": r.get("f14", ""),
                "price": r.get("f2"),
                "pct": r.get("f3"),
                "main_net": r.get("f62"),
            })
        return out
    except Exception as e:
        log.warning("fetch_board_members %s 失败: %s", bk_code, e)
        return []


def match_board(concept: str, board_map: dict) -> tuple:
    """模糊匹配概念名到真实板块。返回 (板块名, BK代码) 或 (None, None)。"""
    if not concept:
        return None, None
    c = concept.strip()
    # 精确
    if c in board_map:
        return c, board_map[c]
    # 一方包含另一方
    for name, bk in board_map.items():
        if c in name or name in c:
            return name, bk
    # 去掉常见后缀再比
    for suffix in ("概念", "板块"):
        if c.endswith(suffix) and c[: -len(suffix)] in board_map:
            n = c[: -len(suffix)]
            return n, board_map[n]
    return None, None


# ---------------- LLM 推理 ----------------


def _extract_json(text: str):
    """从 LLM 输出中提取 JSON(容错 markdown 代码块)。"""
    import re

    text = text.strip()
    text = re.sub(r"^```(json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def extract_events(news: list, decider) -> list[dict]:
    """LLM 第一段推理:从新闻中筛选可交易事件。"""
    lines = []
    for i, n in enumerate(news, 1):
        lines.append(f"{i}. [{n.get('time','')}] {n.get('summary') or n.get('title','')}")
    prompt = f"""你是 A 股市场分析师。从下面当日新闻中筛选出对股市有实际影响的事件(政策发布/订单中标/业绩预告/行业供需变化/技术突破/产品涨价等),忽略纯市场综述、观点评论、数据播报。

当日新闻:
{chr(10).join(lines)}

筛选规则:
- 只保留有明确驱动逻辑的事件
- 每个事件给出受益/受损的 A 股概念板块(用东财标准概念名,如: 低空经济、CPO、半导体、军工、粮食概念)
- significance: 事件对相关板块的影响力 1-10

输出 JSON 数组(按影响力降序,最多 3 个,没有合格事件输出 []):
[{{"idx": 1, "event": "事件一句话", "direction": "利好|利空|中性", "concepts": ["概念名", "概念名"], "significance": 8}}]

只输出 JSON,不要其他文字。"""
    try:
        raw = decider.generate(prompt, timeout=120)
    except Exception as e:
        log.warning("extract_events LLM 失败: %s", e)
        return []
    events = _extract_json(raw)
    if not isinstance(events, list):
        return []
    out = []
    for ev in events:
        if not isinstance(ev, dict) or "event" not in ev:
            continue
        idx = int(ev.get("idx", 0)) - 1
        if 0 <= idx < len(news):
            ev["news"] = news[idx]
        ev.setdefault("concepts", [])
        ev.setdefault("direction", "中性")
        out.append(ev)
    return out


def reason_events(events: list, decider) -> list[dict]:
    """LLM 第二段推理:事件 + 真实股票数据 → 因果链 + 推荐。"""
    blocks = []
    for i, ev in enumerate(events, 1):
        member_lines = []
        for board_name, members in ev.get("boards", []).items():
            if not members:
                continue
            stocks = "、".join(
                f"{m['name']}({m['code']}, 今日{(m.get('pct') or 0):+.2f}%, 主力净流入{(m.get('main_net') or 0)/1e8:.2f}亿)"
                for m in members
            )
            member_lines.append(f"  【{board_name}】{stocks}")
        ev_desc = ev.get("news", {}).get("summary") or ev.get("news", {}).get("title", "")
        blocks.append(
            f"### 事件{i}: {ev['event']}\n方向: {ev['direction']}\n"
            f"新闻原文: {ev_desc}\n相关板块真实成分股(当日数据):\n"
            + ("\n".join(member_lines) if member_lines else "  (无匹配板块)")
        )
    prompt = f"""你是 A 股分析师,基于事件与真实市场数据做多段因果推理。

{chr(10).join(blocks)}

对每个事件输出(markdown):

#### 事件{i}: {{事件名}}
- **因果链**: 事件 → (传导步骤1) → (传导步骤2) → 受益逻辑
- **重点关注**: 1-3 只股票,每只给出:名称(代码) — 推荐理由(必须引用上面真实数据,如主力资金/当日涨幅,结合业务逻辑)
- **置信度**: 高/中/低 + 一句话原因
- **风险**: 该逻辑不成立或反转的条件

要求:
1. 因果链必须逐步展开,不能一句话带过
2. 推荐理由必须结合真实数据,不编造
3. 推荐股票只能来自给出的真实成分股列表,严禁凭记忆添加列表外的股票
4. 标注"(无匹配板块数据)"的事件,只输出因果链和风险,不推荐具体股票
5. 直接输出分析,不要输出思考过程
"""
    try:
        out = decider.generate(prompt, timeout=180)
    except Exception as e:
        log.warning("reason_events LLM 失败: %s", e)
        return f"(LLM 推理失败: {e})"
    # 推理模型思考过程剥离(与 daily_scan 同一逻辑;DS 系列思考常以"好的，"开头)
    import re

    return re.split(
        r"\n\s*(?:Thinking\s*Process|推理过程|好的[，,])", out, maxsplit=1
    )[0].strip()


def run(news_limit=30, max_events=3, decider=None):
    """主流程:新闻 → 事件 → 接地 → 推理。返回 events(含 reasoning)。"""
    from ai_decider import AIDecider
    from news_digest import fetch_news

    if decider is None:
        decider = AIDecider()

    print(f"[{datetime.now():%H:%M:%S}] 抓取新闻...")
    news = fetch_news(news_limit)
    if not news:
        print("未抓到新闻")
        return []
    print(f"新闻 {len(news)} 条")

    print("LLM 第一段推理: 筛选事件...")
    events = extract_events(news, decider)[:max_events]
    if not events:
        print("无可交易事件")
        return []
    print(f"筛出 {len(events)} 个事件")

    print("数据接地: 概念 → 真实成分股...")
    board_map = fetch_board_list("concept")
    industry_map = fetch_board_list("industry")
    for ev in events:
        merged = {**board_map, **industry_map}
        ev["boards"] = {}
        for c in ev.get("concepts", [])[:3]:
            name, bk = match_board(c, merged)
            if bk:
                ev["boards"][name] = fetch_board_members(bk, top_n=5)

    print("LLM 第二段推理: 因果链分析...")
    reasoning = reason_events(events, decider)
    for ev in events:
        ev["reasoning"] = reasoning
    return events


def build_card(events: list, now=None) -> dict:
    """构造飞书卡片。"""
    if now is None:
        now = datetime.now()
    sections = []
    for i, ev in enumerate(events, 1):
        boards = "、".join(ev.get("boards", {}).keys()) or "-"
        sections.append(
            f"**事件{i}: {ev['event']}**({ev['direction']} | 板块: {boards})\n\n{ev.get('reasoning', '')}"
        )
    return {
        "config": {"wide_screen": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔍 新闻掘金·因果推理 {now.strftime('%m-%d %H:%M')}"},
            "template": "violet",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": ("\n\n---\n\n".join(sections))[:4000]}},
        ],
    }


def format_text(events: list) -> str:
    """Bot 用的纯文本输出。"""
    lines = []
    for i, ev in enumerate(events, 1):
        lines.append(f"### 事件{i}: {ev['event']}({ev['direction']})")
        lines.append(ev.get("reasoning", ""))
        lines.append("")
    return "\n".join(lines)


def main():
    import logging as _lg

    _lg.basicConfig(level=_lg.INFO)
    ap = argparse.ArgumentParser(description="新闻驱动选股:多段因果推理")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送")
    args = ap.parse_args()

    events = run()
    if not events:
        return
    print("\n" + format_text(events))
    if args.dry_run:
        print("\n(dry-run, 不推送)")
        return
    from feishu import FeishuBot

    bot = FeishuBot()
    if not bot.enabled:
        print("feishu 未启用,跳过推送")
        return
    resp = bot.send_card(build_card(events))
    print("飞书推送" + ("成功" if resp and resp.get("code") == 0 else f"失败 {resp}"))


if __name__ == "__main__":
    main()
