"""飞书应用 Bot 长连接机器人: 在群里 @机器人 提问,机器人回复。

工作模式: Function Calling Agent (LLM 自主决策调用工具,多步推理)
  用户问题 → LLM 决策 → 调用工具(analyze/market/yujie/portfolio) →
  LLM 整理结果 → 回复用户(可多轮调用)

工具(由 LLM 自动选择调用):
  analyze_stock(code)  个股技术面分析(23策略信号)
  get_market_status()  今日市场概况
  get_yujie_picks()    今日玉姐精选 Top10
  get_portfolio()      当前模拟盘持仓

启动:
  python feishu_bot.py                  前台长连接运行
  python feishu_bot.py --agent "分析茅台" Agent 单次测试
  python feishu_bot.py --once "市场"    关键词路由降级测试

systemd:
  quant-feishu-bot.service (独立于 quant-api.service)

依赖权限(在 https://open.feishu.cn/app/<app_id>/auth 添加):
  - im:message                  读取消息
  - im:message.group_at_msg     在群里 @机器人 时读取消息
  - im:message:send_as_bot      以应用身份发消息
  - im:resource                 读取消息中的资源
"""

import argparse
import json
import logging
import re
import sqlite3
import time
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

# 日志: 控制台 + 轮转文件(5MB×3,总上限 15MB)
_log_dir = Path("/tmp")
_log_file = _log_dir / "feishu_bot.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("feishu_bot")

ENGINE_HOME = Path(__file__).parent
CONFIG_PATH = ENGINE_HOME / "config.json"
REPORTS_DIR = ENGINE_HOME / "reports"
AGENT_DB = ENGINE_HOME / "agent_data.db"

# 命令路由关键字
CMD_ANALYZE = ("分析", "看看", "看看股")
CMD_MARKET = ("市场", "大盘", "行情", "今日")
CMD_YUJIE = ("玉姐", "候选", "精选", "top")
CMD_PORTFOLIO = ("持仓", "portfolio", "仓位", "股票池")

# A股代码正则(6位数字)
RE_CODE = re.compile(r"\b(60[0-3]\d{3}|00[0-2]\d{3}|30[0-4]\d{3}|688\d{3}|8\d{5}|4\d{5})\b")

# handler 调用过程中累积的图片(给 Agent 用,处理完一轮清空)
_pending_images: list[bytes] = []

# 跨轮对话历史持久化(按 session_id 存储,sqlite)
# 保留最近 MAX_HISTORY_TURNS 轮(1 轮 = user + assistant 两条消息)
# 超过 HISTORY_EXPIRE_DAYS 天未活跃的 session 自动清理(启动时跑一次)
HISTORY_DB = ENGINE_HOME / "agent_history.db"
MAX_HISTORY_TURNS = 6
HISTORY_EXPIRE_DAYS = 7


def _load_history(session_id: str) -> list:
    """从 sqlite 加载某会话的对话历史(按 chat_id+sender 组合隔离)。

    Args:
        session_id: 格式 "<chat_id>:<open_id>",同一群里不同用户各自独立
    """
    try:
        conn = sqlite3.connect(str(HISTORY_DB), timeout=5)
        # 表不存在时静默返回 [](首次启动正常情况)
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_history'"
        ).fetchone():
            conn.close()
            return []
        row = conn.execute(
            "SELECT history_json FROM agent_history WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as e:
        log.warning("加载历史失败 %s: %s", session_id, e)
    return []


# 历史里 assistant 消息裁剪阈值(超长截断到摘要,避免回测/策略大全等长回复撑爆历史)
HISTORY_MSG_MAX_CHARS = 500
HISTORY_MSG_KEEP_CHARS = 200


def _truncate_history(history: list) -> list:
    """裁剪历史中的超长 assistant 消息,保留首部 N 字 + 截断标记。

    user 消息通常很短不裁剪,只裁 assistant(可能含完整回测报告/策略列表)。
    """
    out = []
    for m in history:
        c = m.get("content", "")
        if m.get("role") == "assistant" and len(c) > HISTORY_MSG_MAX_CHARS:
            c = c[:HISTORY_MSG_KEEP_CHARS] + "...(已截断)"
            out.append({**m, "content": c})
        else:
            out.append(m)
    return out


def _save_history(session_id: str, history: list) -> None:
    """保存对话历史到 sqlite,只保留最近 MAX_HISTORY_TURNS 轮 + 裁剪超长消息。"""
    try:
        # 限制历史长度:每轮是 user+assistant 2 条,保留最近 N 轮
        max_msgs = MAX_HISTORY_TURNS * 2
        if len(history) > max_msgs:
            history = history[-max_msgs:]
        # 裁剪超长 assistant 消息(省 token + 省 sqlite 体积)
        history = _truncate_history(history)
        conn = sqlite3.connect(str(HISTORY_DB), timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_history (
                session_id TEXT PRIMARY KEY,
                history_json TEXT,
                ts INTEGER
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO agent_history(session_id, history_json, ts) VALUES(?,?,?)",
            (session_id, json.dumps(history, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("保存历史失败 %s: %s", session_id, e)


# 用户重置历史的触发词(整句匹配,大小写不敏感)
# 注意:用精确匹配避免误判"重置BOLL参数"等正常操作
RESET_KEYWORDS = ("重置", "新话题", "忘了吧", "清空", "重新开始", "清空历史",
                  "重置历史", "清空对话", "重置一下", "请重置", "请清空",
                  "清空一下", "忘掉吧", "从头开始",
                  "/reset", "/new", "/clear")


def _is_reset_command(text: str) -> bool:
    """判断用户是否想清空对话历史。整句精确匹配,避免误判正常操作。"""
    t = text.lower().strip()
    # 去掉标点
    t = re.sub(r"[。,.!?！？\s]+", "", t)
    return t in RESET_KEYWORDS


def _clear_history(session_id: str) -> None:
    """清空某会话的对话历史。"""
    try:
        conn = sqlite3.connect(str(HISTORY_DB), timeout=5)
        conn.execute("DELETE FROM agent_history WHERE session_id=?", (session_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("清空历史失败 %s: %s", session_id, e)


def _purge_old_history() -> int:
    """清理超过 HISTORY_EXPIRE_DAYS 天未活跃的会话历史。

    Bot 启动时调用一次,防止 sqlite 长期积累无用 session。
    返回清理的条数。
    """
    try:
        conn = sqlite3.connect(str(HISTORY_DB), timeout=5)
        if not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_history'"
        ).fetchone():
            conn.close()
            return 0
        cutoff = int(time.time()) - HISTORY_EXPIRE_DAYS * 86400
        cur = conn.execute("DELETE FROM agent_history WHERE ts < ?", (cutoff,))
        n = cur.rowcount
        conn.commit()
        conn.close()
        if n > 0:
            log.info("清理过期历史 %d 条(>%d天未活跃)", n, HISTORY_EXPIRE_DAYS)
        return n
    except Exception as e:
        log.warning("清理过期历史失败: %s", e)
        return 0

# 简单股票名称缓存(避免每次都查 DB)
_name_cache: dict[str, str] = {}
_name_cache_loaded = False


# ============ 配置 ============


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


# ============ 股票名称映射 ============


def _ensure_name_cache():
    global _name_cache_loaded
    if _name_cache_loaded:
        return
    cache_db = ENGINE_HOME / "stock_cache.db"
    if not cache_db.exists():
        _name_cache_loaded = True
        return
    try:
        conn = sqlite3.connect(str(cache_db), timeout=5)
        # 尝试从 daily 表里取最新一条对应 code 的 name(若有 name 列)
        rows = conn.execute("SELECT DISTINCT code, name FROM (SELECT code, name FROM daily LIMIT 1)").fetchall()
        conn.close()
        # daily 表可能没有 name 列,这种情况下 cache 留空
        for code, name in rows:
            _name_cache[code] = name
    except Exception:
        pass
    _name_cache_loaded = True


def resolve_code(text: str) -> str | None:
    """从用户文本里提取股票代码。支持直接 6 位代码或名称(查 daily 表)。"""
    m = RE_CODE.search(text)
    if m:
        return m.group(1)
    # 名称查找:从 stock_cache.db daily 表 LIKE 匹配
    cache_db = ENGINE_HOME / "stock_cache.db"
    if not cache_db.exists():
        return None
    try:
        conn = sqlite3.connect(str(cache_db), timeout=5)
        # daily 表结构: date, code, open, close, high, low, volume, amount
        # 没有 name 列,只能通过其他方式。先用代码本身或 common 名称硬编码常见股
        conn.close()
    except Exception:
        pass
    # 常见股票硬编码(可后续扩展)
    well_known = {
        "茅台": "600519", "贵州茅台": "600519",
        "五粮液": "000858",
        "宁德时代": "300750",
        "比亚迪": "002594",
        "平安银行": "000001", "平安": "000001",
        "招商银行": "600036", "招行": "600036",
        "中信证券": "600030",
        "京东方": "000725",
        "恒瑞医药": "600276",
        "海康威视": "002415",
        "美的集团": "000333",
        "格力电器": "000651",
        "万科": "000002", "万科A": "000002",
        "中石油": "601857", "中国石油": "601857",
        "工商银行": "601398", "工行": "601398",
        "建设银行": "601939", "建行": "601939",
    }
    for name, code in well_known.items():
        if name in text:
            return code
    return None


# ============ 命令分发 ============


def route(text: str) -> tuple[str, str]:
    """根据文本路由到对应处理器。

    Returns:
        (handler_name, formatted_reply)
    """
    text = text.strip()
    if not text:
        return "ai", "请告诉我您要查询的内容,例如:\n- 分析 600519\n- 市场\n- 玉姐\n- 持仓"

    # 1. 个股分析(优先级最高,含 6 位代码或常见股名)
    code = resolve_code(text)
    if code and any(k in text for k in CMD_ANALYZE) or (code and not any(k in text for k in CMD_MARKET + CMD_YUJIE + CMD_PORTFOLIO)):
        return "analyze", handler_analyze(code)

    # 2. 市场概况
    if any(k in text for k in CMD_MARKET):
        return "market", handler_market()

    # 3. 玉姐候选
    if any(k in text for k in CMD_YUJIE):
        return "yujie", handler_yujie()

    # 4. 持仓查询
    if any(k in text for k in CMD_PORTFOLIO):
        return "portfolio", handler_portfolio()

    # 5. AI 自由问答
    return "ai", handler_ai(text)


# ============ Handlers ============


def handler_analyze(code: str) -> str:
    """个股技术面分析。返回文本(可选附带图片通过 _send_after_handler)。"""
    try:
        from strategy_engine import analyze
        r = analyze(code, use_ai=False)
        if "error" in r:
            return f"❌ {code} 分析失败: {r['error']}"
        s = r.get("summary", {})
        verdict = r.get("verdict", "-")
        rt = r.get("realtime") or {}
        price = rt.get("price", 0)
        pct = rt.get("pct", 0)
        emoji = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"

        # 生成 K 线图(异步可优化,这里同步)
        try:
            from feishu_image import gen_kline_chart
            img = gen_kline_chart(code)
            if img:
                _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成 K 线图失败 %s: %s", code, e)

        lines = [
            f"📊 {code} {rt.get('name', '')} {emoji} {price:.2f} ({pct:+.2f}%)",
            f"综合判断: **{verdict}** (买{s.get('buy',0)}/卖{s.get('sell',0)}/观{s.get('hold',0)})",
            "",
        ]
        buys = r.get("buy_reasons", [])[:5]
        sells = r.get("sell_reasons", [])[:5]
        if buys:
            lines.append("✅ 买入信号:")
            for b in buys:
                lines.append(f"  • {b.get('name','')}: {b.get('reason','')}")
        if sells:
            lines.append("⚠️ 卖出信号:")
            for s in sells:
                lines.append(f"  • {s.get('name','')}: {s.get('reason','')}")
        if not buys and not sells:
            lines.append("(无明显信号)")
        if _pending_images:
            lines.append("\n[已附 K 线+指标图]")
        return "\n".join(lines)
    except Exception as e:
        log.error("analyze 异常: %s\n%s", e, traceback.format_exc())
        return f"❌ 分析 {code} 出错: {e}"


def handler_market() -> str:
    """今日市场概况。优先读今日日报,否则提示用户先跑 daily_scan。"""
    today = datetime.now().strftime("%Y%m%d")
    report = REPORTS_DIR / f"daily_{today}.md"
    if not report.exists():
        return (
            "今日市场日报尚未生成。\n"
            "运行 `python daily_scan.py` 生成,或等待 09:00 自动任务。\n"
            "如需实时抓取,请直接输入股票代码进行分析。"
        )
    try:
        text = report.read_text(encoding="utf-8")
        # 提取「一、全市场扫描」段 + 解析数据生成情绪图
        m = re.search(r"## 一、全市场扫描(.*?)## 二", text, re.S)
        section = m.group(1) if m else ""
        # 尝试从 section 解析数字生成图
        try:
            from feishu_image import gen_market_chart
            stats = _parse_market_from_report(section)
            if stats:
                img = gen_market_chart(stats)
                if img:
                    _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成市场图失败: %s", e)
        if section:
            text_out = "📊 今日市场概况\n" + section.strip()
            if _pending_images:
                text_out += "\n[已附市场情绪图]"
            return text_out
        return "📊 今日日报已生成,但格式异常。请查看: " + str(report)
    except Exception as e:
        return f"❌ 读取日报出错: {e}"


def _parse_market_from_report(section: str) -> dict | None:
    """从日报 markdown 段落解析市场统计数据。"""
    try:
        # "总数 **5544** 只：上涨 1655 / 下跌 3774 / 平 115"
        m = re.search(r"总数\s*\*?(\d+)\*?\s*只[：:]\s*上涨\s*(\d+)\s*/\s*下跌\s*(\d+)\s*/\s*平\s*(\d+)", section)
        if not m:
            return None
        total, up, dn, flat = [int(x) for x in m.groups()]
        m2 = re.search(r"涨停\s*\*?(\d+)\*?\s*只[，,]?\s*跌停\s*\*?(\d+)\*?\s*只", section)
        lu, ld = [int(x) for x in m2.groups()] if m2 else (0, 0)
        m3 = re.search(r"成交额约\s*\*?([\d.]+)\*?\s*亿", section)
        amt = float(m3.group(1)) if m3 else 0
        return {"total": total, "up": up, "down": dn, "flat": flat,
                "limit_up": lu, "limit_down": ld, "total_amount_yi": amt}
    except Exception:
        return None


def handler_yujie(min_score: float = 0, hit_rule: str = "") -> str:
    """今日玉姐精选 Top10,支持按最低评分和命中规则过滤。"""
    try:
        import yujie_scan
        picks = yujie_scan.load_picks()
        if not picks:
            return (
                "今日玉姐精选尚未生成。\n"
                "运行 `python yujie_scan.py` 或等待 09:00 自动任务。"
            )

        # 过滤
        filtered = picks
        if min_score > 0:
            filtered = [p for p in filtered if p.get("score", 0) >= min_score]
        if hit_rule:
            filtered = [p for p in filtered if hit_rule in (p.get("hits") or [])]

        if not filtered:
            cond = []
            if min_score > 0:
                cond.append(f"score>={min_score}")
            if hit_rule:
                cond.append(f"命中'{hit_rule}'")
            return f"❌ 今日玉姐精选无匹配(条件: {', '.join(cond) or '无'})"

        # 默认只看 Top10(排名前10);有过滤条件时显示过滤后的全部(最多20)
        has_filter = min_score > 0 or bool(hit_rule)
        show = filtered if has_filter else filtered[:10]
        show = show[:20]  # 最多20只避免消息过长

        # 生成候选墙图
        try:
            from feishu_image import gen_yujie_wall
            img = gen_yujie_wall(show)
            if img:
                _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成玉姐墙失败: %s", e)

        # 描述过滤条件
        cond_str = ""
        if has_filter:
            parts = []
            if min_score > 0:
                parts.append(f"≥{min_score:g}分")
            if hit_rule:
                parts.append(f"命中'{hit_rule}'")
            cond_str = f"(过滤: {'、'.join(parts)})"
        else:
            cond_str = " Top10"

        lines = [f"🎯 今日玉姐精选{cond_str} 共 {len(filtered)} 只"]
        for p in show:
            hits = "、".join(p.get("hits", [])) if p.get("hits") else "无命中"
            lines.append(
                f"{p['rank']}. **{p['code']} {p['name']}** | {p['score']:g}分 | {hits}"
            )
        if len(filtered) > len(show):
            lines.append(f"\n(共 {len(filtered)} 只,仅显示前 {len(show)} 只)")
        if _pending_images:
            lines.append("\n[已附候选 K 线缩略图墙]")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 读取玉姐精选出错: {e}"


def handler_portfolio() -> str:
    """持仓查询。"""
    if not AGENT_DB.exists():
        return "📭 暂无持仓数据库(agent_data.db 不存在)。"
    try:
        conn = sqlite3.connect(str(AGENT_DB), timeout=5)
        rows = conn.execute("SELECT code, qty, cost, buy_date FROM positions").fetchall()
        conn.close()
        if not rows:
            return "📭 当前无持仓。"
        lines = ["💼 当前持仓:"]
        total_cost = 0.0
        for code, qty, cost, buy_date in rows:
            lines.append(f"  • {code}  {qty}股 @ {cost:.2f}  (买入日 {buy_date})")
            total_cost += cost * qty
        lines.append(f"\n总成本: {total_cost:.2f}")
        lines.append("(实时市值需联网查价,请用 /api/portfolio 接口获取完整盈亏)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询持仓出错: {e}"


def handler_ai(text: str) -> str:
    """AI 自由问答。"""
    try:
        from ai_decider import AIDecider
        decider = AIDecider()
        prompt = f"你是 A 股量化助手,用户在飞书提问,请简明回答(200 字内):\n\n{text}"
        resp = decider.generate(prompt, timeout=60)
        if resp.startswith(("API限流", "API错误", "调用失败")):
            return f"❌ AI 调用失败: {resp}"
        # 去除思考过程
        resp = re.split(r"\n\s*(?:Thinking\s*Process|推理过程)[:：]", resp)[0].strip()
        return "🤖 " + resp
    except Exception as e:
        return f"❌ AI 问答出错: {e}"


# ============ Agent: Function Calling ReAct ============


# 工具 schema(OpenAI function calling 格式)
TOOLS = [
    # ---------- 个股/市场/选股/持仓 ----------
    {
        "type": "function",
        "function": {
            "name": "analyze_stock",
            "description": "对指定A股代码做技术面分析,返回综合判断+买入/卖出信号(基于当前已启用的策略)。用户问'分析X/看看X/X怎样'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "6位A股代码,如600519(茅台)、000001(平安银行)、300750(宁德时代)"
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_status",
            "description": "获取今日A股全市场概况:涨跌停分布、成交额、市场情绪。用户问'市场/大盘/行情/今天怎样'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_yujie_picks",
            "description": "获取今日玉姐精选 Top10 候选股(多因子评分排行,含命中规则)。用户问'玉姐/候选/精选/top/选股'时调用。支持按最低评分和命中规则过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_score": {
                        "type": "number",
                        "description": "最低评分门槛,只返回≥此分的股票。如 7=只看7+分强势股,5=玉姐精选默认门槛。不传=不过滤",
                        "default": 0
                    },
                    "hit_rule": {
                        "type": "string",
                        "description": "按命中规则过滤,只返回命中该规则的股票。规则名(中文)如 'MACD金叉'/'突破+金叉'/'RSI金叉'/'多线多头'/'深回撤'/'MOS低点'。不传=不过滤"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "查询当前模拟盘持仓:股票代码、数量、成本、买入日期。用户问'持仓/仓位/portfolio/股票池'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    # ---------- 策略管理 skill ----------
    {
        "type": "function",
        "function": {
            "name": "list_strategies",
            "description": "列出所有策略(23个内置+自定义),含开关状态、当前参数、回测超额收益。用户问'有哪些策略/策略状态/策略列表/哪些策略开了'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_strategy_library",
            "description": "查策略大全4来源(漫画书/操练大全/玉姐精选/AI)中的策略,支持多维度过滤。用户问'策略大全/操练大全/漫画书策略/某书第X章/某策略在哪些书里/某书里哪些没实现'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["book_cartoon", "book_caolian", "yujie_custom", "ai_custom", ""],
                        "description": "来源过滤,空字符串=全部。book_cartoon=半小时漫画股票实战法、book_caolian=中国股市操练大全、yujie_custom=玉姐精选、ai_custom=AI自定义"
                    },
                    "category": {
                        "type": "string",
                        "description": "章节/分类名模糊匹配(子串包含),如'第15章'或'抄底'。空字符串=不过滤"
                    },
                    "implemented_only": {
                        "type": "boolean",
                        "description": "true=只看已实现,false=只看未实现,不传=全部"
                    },
                    "include_meta": {
                        "type": "boolean",
                        "description": "true=附带书的元数据(作者/简介/章节数/文件列表)"
                    },
                    "cross_ref": {
                        "type": "string",
                        "description": "跨来源对比:传策略id(如macd),返回该策略在哪些来源/章节出现。优先级高于其他过滤参数"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_yujie_detail",
            "description": "查询玉姐精选的详细信息:10条评分规则、每条score权重、回测表现。用户问'玉姐评分规则/玉姐怎么打分/玉姐回测表现'时调用。",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_with_strategy",
            "description": "用指定策略单独分析个股(只看该策略的信号,不跑全部23个)。用户问'看X的MACD/KDJ/BOLL信号'、'用某策略分析X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位A股代码"},
                    "strategy_id": {
                        "type": "string",
                        "description": "策略id,如 macd/kdj/boll/rsi/dmi/bottom/top/zt 等"
                    }
                },
                "required": ["code", "strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_with_yujie",
            "description": "用玉姐精选10条评分规则分析个股,给出综合评分+命中规则+未命中规则+解读。用户问'用玉姐分析X/玉姐评分看X/玉姐策略测X'时调用。与 analyze_with_strategy 不同:玉姐是复合评分体系(10条规则累加),不是单策略买卖信号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6位A股代码"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_strategy",
            "description": "开启或关闭某策略(修改 config.json,影响后续 analyze 的信号)。用户明确说'关闭X策略/打开X策略/启用X'时调用。操作类,LLM 需确认用户意图明确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id,如 macd/kdj/ma_combo"},
                    "enabled": {"type": "boolean", "description": "true=开启,false=关闭"}
                },
                "required": ["strategy_id", "enabled"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_strategy_params",
            "description": "调整策略参数(修改 config.json,影响后续 analyze)。用户明确说'把BOLL周期改成X/MACD signal改成X'时调用。操作类,LLM 需确认用户意图明确。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id"},
                    "params": {
                        "type": "object",
                        "description": "参数键值对,如 {\"period\": 30, \"std\": 2.5}"
                    }
                },
                "required": ["strategy_id", "params"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "enable_library_strategy",
            "description": "从策略大全引入一个未实现的策略(标记为已实现并启用)。仅对 strategy_library.json 中 implemented=false 的策略有意义。用户说'引入X策略/启用大全里的X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "library_id": {"type": "string", "description": "策略大全中的策略id,如 bottom_kline/top_volume"}
                },
                "required": ["library_id"]
            }
        }
    },
    # ---------- 回测/寻优 skill(耗时操作) ----------
    {
        "type": "function",
        "function": {
            "name": "backtest_strategy",
            "description": "对指定策略做全市场回测,返回超额 alpha 收益(相对基准)。耗时约 1-2 分钟,会先返回'开始回测'提示。用户问'回测X策略/X策略表现怎样/X策略历史收益'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "description": "策略id(必须是内置策略)"},
                    "sample": {"type": "integer", "description": "抽样股票数,默认 0=全市场4376只。调试可用 200", "default": 0}
                },
                "required": ["strategy_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grid_search_strategy",
            "description": "对指定策略做参数网格寻优,返回最优参数组合。耗时约 2-5 分钟。仅支持 macd/kdj/boll/dmi 四策略。用户问'寻优X策略/X策略最优参数/调参X'时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {"type": "string", "enum": ["macd", "kdj", "boll", "dmi"], "description": "策略id"},
                    "sample": {"type": "integer", "description": "抽样股票数,默认 400", "default": 400}
                },
                "required": ["strategy_id"]
            }
        }
    },
]


def handler_list_strategies() -> str:
    """列出所有策略及状态。"""
    try:
        import strategy_engine as se
        strategies = se.get_strategies()
        # 加载回测报告(若存在)取超额收益
        excess_map = {}
        report_json = ENGINE_HOME / "builtin_backtest_report.json"
        if report_json.exists():
            try:
                rep = json.loads(report_json.read_text(encoding="utf-8"))
                for sid, s in rep.get("strategies", {}).items():
                    # horizons: {"60": {"excess": 0.0088, ...}}
                    h = s.get("horizons", {})
                    h60 = h.get("60", {}).get("excess")
                    h20 = h.get("20", {}).get("excess")
                    excess_map[sid] = (h60 or h20 or 0) * 100  # 转百分比
            except Exception:
                pass

        lines = ["📋 **当前策略状态**\n"]
        lines.append("| ID | 名称 | 开关 | 关键参数 | 60天超额 |")
        lines.append("|---|---|---|---|---|")
        builtin_ids = set()
        for s in strategies:
            sid = s.get("id", "")
            name = s.get("name", sid)
            enabled = "✅" if s.get("enabled", True) else "❌"
            params = s.get("params", {})
            # 只显示 2 个关键参数,避免表格过宽
            p_str = ", ".join(f"{k}={v}" for k, v in list(params.items())[:2]) if params else "-"
            excess = excess_map.get(sid)
            excess_str = f"{excess:+.2f}%" if excess is not None else "-"
            if s.get("type") == "builtin" or sid in (
                "macd", "kdj", "ma_stop", "boll", "dmi", "psy", "bias", "sar",
                "bbiboll", "tower", "ma_combo", "two_line", "life_line",
                "three_third", "sparrow", "bounce", "volume_div", "resonance",
                "dmi_psy", "rsi", "bottom", "top", "zt",
            ):
                builtin_ids.add(sid)
            lines.append(f"| {sid} | {name} | {enabled} | {p_str} | {excess_str} |")

        lines.append(f"\n共 {len(strategies)} 个策略(23 内置 + 自定义)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 列出策略出错: {e}"


def _cross_ref_search(strategy_id: str, lib: dict) -> str:
    """跨来源反向索引: 查 strategy_id 在哪些来源/章节出现(匹配 id 或 engine_id)。"""
    hits = []
    target = strategy_id.lower()
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                sid = (st.get("id") or "").lower()
                eid = (st.get("engine_id") or "").lower()
                # 精确匹配 id 或 engine_id,或者 id 以 strategy_id 为前缀(如 macd_8 匹配 macd)
                if target == sid or target == eid or sid.startswith(target + "_") or eid == target:
                    hits.append({
                        "source_id": src.get("id", ""),
                        "source_name": src.get("name", ""),
                        "category": cat.get("name", ""),
                        "strategy_id": st.get("id", ""),
                        "strategy_name": st.get("name", ""),
                        "implemented": st.get("implemented", False),
                        "engine_id": st.get("engine_id", ""),
                        "desc": st.get("desc", ""),
                    })
    if not hits:
        return f"❌ 跨来源搜索未找到策略 {strategy_id}"
    lines = [f"🔍 **跨来源搜索: {strategy_id}**\n共在 {len(hits)} 处出现:"]
    by_source: dict[str, list] = {}
    for h in hits:
        by_source.setdefault(h["source_name"], []).append(h)
    for src_name, items in by_source.items():
        lines.append(f"\n📚 **{src_name}**")
        for h in items:
            mark = "✅" if h["implemented"] else "⬜"
            engine_str = f" → engine: {h['engine_id']}" if h["engine_id"] and h["engine_id"] != h["strategy_id"] else ""
            lines.append(f"  - {mark} [{h['category']}] **{h['strategy_id']}**{engine_str}: {h['desc']}")
    return "\n".join(lines)


def handler_get_strategy_library(
    source: str = "",
    category: str = "",
    implemented_only: bool | None = None,
    include_meta: bool = False,
    cross_ref: str = "",
) -> str:
    """查策略大全,支持多维度过滤 + 跨来源反向索引。

    Args:
        source: 来源 id 过滤,空=全部
        category: 章节/分类名模糊匹配(子串包含),空=不过滤
        implemented_only: True=只看已实现, False=只看未实现, None=全部
        include_meta: True=附带书的元数据(作者/简介/章节列表)
        cross_ref: 策略 id,跨来源反向搜索(优先级最高)
    """
    try:
        lib_path = ENGINE_HOME / "strategy_library.json"
        if not lib_path.exists():
            return "❌ 策略大全数据不存在(strategy_library.json)"
        lib = json.loads(lib_path.read_text(encoding="utf-8"))

        # 优先处理跨来源搜索
        if cross_ref:
            return _cross_ref_search(cross_ref, lib)

        lines = []
        total_shown = 0
        for src in lib.get("sources", []):
            sid = src.get("id", "")
            if source and sid != source:
                continue

            # include_meta: 输出书的元数据
            if include_meta:
                lines.append(f"\n## 📚 {src['name']}")
                lines.append(f"- 作者: {src.get('author', '-')}")
                lines.append(f"- 类型: {src.get('type', '-')}")
                lines.append(f"- 状态: {src.get('status', '-')}")
                summary = src.get("summary", "")
                if summary:
                    lines.append(f"- 简介: {summary}")
                cats = src.get("categories", [])
                lines.append(f"- 章节数: {len(cats)}")
                cat_names = "、".join(c.get("name", "") for c in cats)
                lines.append(f"- 章节: {cat_names}")
                files = src.get("files", [])
                if files:
                    lines.append(f"- 关联文件: {', '.join(files[:3])}{'...' if len(files)>3 else ''}")
                stats = src.get("stats", {})
                if stats:
                    lines.append(f"- 统计: 实现 {stats.get('implemented','-')}/{stats.get('total_chapters','-')}")
                lines.append("")

            # 收集本来源下匹配的策略
            src_lines = []
            src_count = 0
            for cat in src.get("categories", []):
                cat_name = cat.get("name", "")
                if category and category not in cat_name:
                    continue
                cat_strategies = []
                for st in cat.get("strategies", []):
                    impl = st.get("implemented", False)
                    if implemented_only is True and not impl:
                        continue
                    if implemented_only is False and impl:
                        continue
                    mark = "✅" if impl else "⬜"
                    engine_id = st.get("engine_id", "")
                    engine_str = f" → {engine_id}" if engine_id else ""
                    cat_strategies.append(f"  - {mark} **{st['id']}**{engine_str}: {st.get('desc', '')}")
                if cat_strategies:
                    cat_label = cat_name
                    if not include_meta:
                        cat_label += f" ({cat.get('book_category', '')})" if cat.get("book_category") else ""
                    src_lines.append(f"\n### {cat_label}")
                    src_lines.extend(cat_strategies)
                    src_count += len(cat_strategies)

            if src_count == 0 and (category or implemented_only is not None):
                # 本来源过滤后无匹配,不输出
                continue

            if not include_meta:
                lines.append(f"\n## 📚 {src['name']}")
            lines.extend(src_lines)
            total_shown += src_count

        if total_shown == 0:
            filters = []
            if source:
                filters.append(f"source={source}")
            if category:
                filters.append(f"category~={category}")
            if implemented_only is True:
                filters.append("implemented=true")
            if implemented_only is False:
                filters.append("implemented=false")
            return f"❌ 无匹配策略(过滤条件: {', '.join(filters) or '无'})"

        # 末尾统计
        lines.append(f"\n---\n共显示 {total_shown} 个策略")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查策略大全出错: {e}"


def handler_get_yujie_detail() -> str:
    """查询玉姐精选详细:评分规则 + score 权重 + 回测表现。"""
    try:
        lib_path = ENGINE_HOME / "strategy_library.json"
        if not lib_path.exists():
            return "❌ 策略大全数据不存在"
        lib = json.loads(lib_path.read_text(encoding="utf-8"))
        yujie = next((s for s in lib.get("sources", []) if s.get("id") == "yujie_custom"), None)
        if not yujie:
            return "❌ 未找到玉姐精选来源"

        lines = [f"## 🎯 {yujie['name']}"]
        summary = yujie.get("summary", "")
        if summary:
            lines.append(f"\n{summary}")

        # 评分规则
        lines.append("\n### 评分规则")
        lines.append("| 规则ID | 名称 | 分数 | 说明 |")
        lines.append("|---|---|---|---|")
        for cat in yujie.get("categories", []):
            for st in cat.get("strategies", []):
                score = st.get("score", 0)
                lines.append(f"| {st['id']} | {st['name']} | +{score} | {st.get('desc', '')} |")

        # 回测
        bt = yujie.get("backtest", {})
        if bt:
            lines.append("\n### 回测表现")
            lines.append(f"- 信号样本数: {bt.get('signal_count', '-')}")
            lines.append(f"- 验证结论: {bt.get('validated', '-')}")

        # 当前参数(从 config.json)
        try:
            import strategy_engine as se
            cfg = se._load_config()
            ycfg = cfg.get("yujie_agent", {})
            if ycfg:
                lines.append("\n### 当前调度参数(yujie_agent)")
                lines.append(f"- 最低评分门槛: {ycfg.get('min_score', '-')}")
                lines.append(f"- 最大持有天数: {ycfg.get('max_hold_days', '-')}")
        except Exception:
            pass

        lines.append("\n💡 玉姐精选是复合评分体系,通过 `分析` 命令触发时会综合其他策略信号。")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查玉姐详情出错: {e}"


def _lookup_library_strategy(strategy_id: str) -> list[dict]:
    """在策略大全里查 strategy_id,返回所有命中的策略条目(可能多来源都有)。

    匹配规则:精确匹配 id 或 engine_id,或 id 以 strategy_id 为前缀(如 macd_8 匹配 macd)。
    每条返回 {source_id, source_name, category, id, name, engine_id, implemented, desc}
    """
    lib_path = ENGINE_HOME / "strategy_library.json"
    if not lib_path.exists():
        return []
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    target = strategy_id.lower()
    hits = []
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                sid = (st.get("id") or "").lower()
                eid = (st.get("engine_id") or "").lower()
                if target == sid or target == eid or sid.startswith(target + "_") or eid == target:
                    hits.append({
                        "source_id": src.get("id", ""),
                        "source_name": src.get("name", ""),
                        "category": cat.get("name", ""),
                        "id": st.get("id", ""),
                        "name": st.get("name", ""),
                        "engine_id": st.get("engine_id", ""),
                        "implemented": st.get("implemented", False),
                        "desc": st.get("desc", ""),
                    })
    return hits


def handler_analyze_with_strategy(code: str, strategy_id: str) -> str:
    """用指定策略分析个股,联动策略大全给出"来源+核心逻辑+当前信号+理由"。

    流程:
    1. 在策略大全里查 strategy_id(精确/前缀匹配),拿到来源、描述、engine_id
    2. 若已实现: 跑 analyze,取 engine_id 对应的信号,输出一条龙
    3. 若未实现: 告知用户该策略尚未实现,列出最接近的已实现策略作为替代
    4. 若查不到: 走老逻辑直接用 strategy_id 跑 analyze
    """
    try:
        import strategy_engine as se

        # 1. 查策略大全
        hits = _lookup_library_strategy(strategy_id)

        # 2. 决定实际引擎策略 id
        engine_id = strategy_id
        lib_section = ""
        if hits:
            # 优先选已实现的命中
            impl_hits = [h for h in hits if h["implemented"]]
            if impl_hits:
                h = impl_hits[0]
                engine_id = h["engine_id"] or h["id"]
                # 来源 + 核心逻辑
                src_lines = []
                for hh in impl_hits:
                    src_lines.append(
                        f"  - 📚 {hh['source_name']} · {hh['category']}: **{hh['name']}**"
                    )
                lib_section = (
                    "\n📖 **策略来源**\n" + "\n".join(src_lines) +
                    f"\n\n📝 **核心逻辑**: {h['desc']}"
                )
            else:
                # 全部未实现
                h = hits[0]
                src_lines = []
                for hh in hits:
                    src_lines.append(f"  - 📚 {hh['source_name']} · {hh['category']}: {hh['name']}")
                lib_section = (
                    "\n📖 **策略来源**\n" + "\n".join(src_lines) +
                    f"\n\n📝 **核心逻辑**: {h['desc']}"
                    f"\n\n⚠️ **该策略尚未实现**,无法直接分析。"
                )
                # 找一个相近的已实现策略作替代建议
                # 简单启发:若 strategy_id 含某关键字(如 bottom/top/macd/kdj/boll),给对应已实现策略
                _ALIAS = {
                    "bottom": "bottom", "抄底": "bottom",
                    "top": "top", "逃顶": "top",
                    "zt": "zt", "涨停": "zt",
                    "macd": "macd", "kdj": "kdj", "boll": "boll",
                    "rsi": "rsi", "dmi": "dmi", "bias": "bias", "sar": "sar",
                }
                suggest = ""
                for kw, sid in _ALIAS.items():
                    if kw in strategy_id.lower() or kw in h.get("name", "").lower() or kw in h.get("desc", "").lower():
                        suggest = sid
                        break
                if suggest:
                    lib_section += f"\n💡 可用相近策略 **{suggest}** 替代,如需分析请说\"用 {suggest} 分析 {code}\"。"
                return lib_section

        # 3. 跑 analyze
        r = se.analyze(code, use_ai=False)
        if "error" in r:
            return f"❌ {code} 分析失败: {r['error']}"
        signals = r.get("signals", [])
        target = next((s for s in signals if s.get("key") == engine_id), None)
        if not target:
            avail = ", ".join(s.get("key", "") for s in signals)
            return f"❌ 未找到策略 {strategy_id}(engine_id={engine_id}),可用策略: {avail}"

        # 4. 组装结果
        rt = r.get("realtime") or {}
        price = rt.get("price", 0)
        pct = rt.get("pct", 0)
        emoji = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"
        sig_emoji = {"buy": "✅买入", "sell": "⚠️卖出", "hold": "➡️观望"}.get(
            target.get("signal", "hold"), ""
        )

        # 生成 K 线图
        try:
            from feishu_image import gen_kline_chart
            img = gen_kline_chart(code)
            if img:
                _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成 K 线图失败 %s: %s", code, e)

        out = (
            f"📊 {code} {rt.get('name', '')} {emoji} {price:.2f} ({pct:+.2f}%)\n"
            f"**策略 {target.get('name', strategy_id)}**: {sig_emoji}\n"
            f"理由: {target.get('reason', '')}"
            f"{lib_section}"
        )
        if _pending_images:
            out += "\n[已附 K 线+指标图]"
        return out
    except Exception as e:
        return f"❌ 分析 {code} 出错: {e}"


def handler_analyze_with_yujie(code: str) -> str:
    """用玉姐精选10条评分规则分析个股,给出综合评分+命中规则+未命中规则+解读。

    与 analyze_with_strategy 不同:玉姐是复合评分体系(10条规则累加分数),
    不是单策略买卖信号,所以需要独立 handler。
    """
    try:
        import yujie_scan

        # 1. 从 strategy_library.json 拿 10 条规则的 (rule_id, name, score, desc)
        lib_path = ENGINE_HOME / "strategy_library.json"
        if not lib_path.exists():
            return "❌ 策略大全数据不存在"
        lib = json.loads(lib_path.read_text(encoding="utf-8"))
        yujie_src = next((s for s in lib.get("sources", []) if s.get("id") == "yujie_custom"), None)
        if not yujie_src:
            return "❌ 未找到玉姐精选来源"
        rules: list[dict] = []  # [{rule_id, name, score, desc}]
        for cat in yujie_src.get("categories", []):
            for st in cat.get("strategies", []):
                sid = st.get("id", "")
                rule_id = sid[6:] if sid.startswith("yujie_") else sid  # 去 yujie_ 前缀
                rules.append({
                    "rule_id": rule_id,
                    "name": st.get("name", ""),
                    "score": st.get("score", 0),
                    "desc": st.get("desc", ""),
                })
        if not rules:
            return "❌ 玉姐精选规则数据为空"

        # 2. 调 score_stock 打分
        params = yujie_scan.get_params()
        score, hits, detail = yujie_scan.score_stock(code, params)
        if detail is None:
            min_days = params.get("scope", {}).get("min_history_days", 60)
            return f"❌ {code} 数据不足(需 ≥{min_days} 个交易日),无法用玉姐规则分析"

        # 2.5 生成玉姐专属图(K线+评分命中标注)
        try:
            from feishu_image import gen_yujie_chart
            img = gen_yujie_chart(code, score, hits, detail)
            if img:
                _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成玉姐图失败 %s: %s", code, e)

        # 3. 拿实时价格
        import strategy_engine as se
        rt = {}
        try:
            r = se.analyze(code, use_ai=False)
            rt = r.get("realtime") or {}
        except Exception:
            pass
        price = rt.get("price", detail.get("price", 0))
        pct = rt.get("pct", 0)
        name = rt.get("name", "")
        emoji = "📈" if pct > 0 else "📉" if pct < 0 else "➡️"

        # 4. 命中/未命中分组(用 detail 的 bool 字段判断,比 hits 的中文 label 更稳)
        hit_rules = []
        miss_rules = []
        for r in rules:
            rid = r["rule_id"]
            if detail.get(rid):
                hit_rules.append(r)
            else:
                miss_rules.append(r)
        total_possible = sum(r["score"] for r in rules)

        # 5. 组装输出
        lines = [
            f"📊 {code} {name} {emoji} {price:.2f} ({pct:+.2f}%)",
            f"🎯 **玉姐评分: {score:g} 分** / 满分 {total_possible:g} 分",
        ]

        if hit_rules:
            lines.append(f"\n✅ **命中规则**({len(hit_rules)}条,共{sum(r['score'] for r in hit_rules):g}分)")
            for r in hit_rules:
                lines.append(f"- {r['name']} (+{r['score']}): {r['desc']}")

        if miss_rules:
            lines.append(f"\n⚪ **未命中规则**({len(miss_rules)}条)")
            for r in miss_rules:
                lines.append(f"- {r['name']} (+{r['score']}): {r['desc']}")

        # 6. 评分解读
        if score >= 7:
            comment = f"🚀 **强势**({score:g}分),玉姐精选 7+ 分档历史 60 天超额 +11.79%"
        elif score >= 5:
            comment = f"📊 **中等偏强**({score:g}分),玉姐精选通常 5+ 分入选"
        elif score >= 3:
            comment = f"⚠️ **偏弱**({score:g}分),低于玉姐精选 5 分入选门槛"
        else:
            comment = f"❌ **弱**({score:g}分),暂不符合玉姐精选标准"
        lines.append(f"\n💡 {comment}")

        lines.append(
            "\n⚠️ 风险提示:玉姐评分为技术面多因子打分,不构成投资建议,请结合基本面与市场情绪综合判断。"
        )
        if _pending_images:
            lines.append("\n[已附玉姐专属图: K线+评分标注]")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 玉姐分析 {code} 出错: {e}"


def handler_toggle_strategy(strategy_id: str, enabled: bool) -> str:
    """开关策略。"""
    try:
        import strategy_engine as se
        strategies = se.get_strategies()
        found = False
        for s in strategies:
            if s.get("id") == strategy_id:
                s["enabled"] = enabled
                found = True
                break
        if not found:
            # 内置策略未在 config 中,追加一条
            strategies.append({"id": strategy_id, "type": "builtin", "enabled": enabled, "params": {}})
            found = True
        se.save_strategies(strategies)
        se.clear_ai_cache()
        action = "已开启" if enabled else "已关闭"
        return f"✅ 策略 {strategy_id} {action}(已写入 config.json,后续 analyze 生效)"
    except Exception as e:
        return f"❌ 切换策略出错: {e}"


def handler_set_strategy_params(strategy_id: str, params: dict) -> str:
    """调整策略参数。"""
    try:
        import strategy_engine as se
        if not params or not isinstance(params, dict):
            return "❌ 参数必须是非空 dict,如 {\"period\": 30}"
        strategies = se.get_strategies()
        found = False
        for s in strategies:
            if s.get("id") == strategy_id:
                cur = s.get("params", {}) or {}
                cur.update(params)
                s["params"] = cur
                found = True
                break
        if not found:
            strategies.append({"id": strategy_id, "type": "builtin", "enabled": True, "params": params})
            found = True
        se.save_strategies(strategies)
        se.clear_ai_cache()
        p_str = ", ".join(f"{k}={v}" for k, v in params.items())
        return f"✅ 策略 {strategy_id} 参数已更新: {p_str}(已写入 config.json,后续 analyze 生效)"
    except Exception as e:
        return f"❌ 调整参数出错: {e}"


def handler_enable_library_strategy(library_id: str) -> str:
    """从策略大全引入一个策略(标记为已实现)。"""
    try:
        lib_path = ENGINE_HOME / "strategy_library.json"
        if not lib_path.exists():
            return "❌ 策略大全数据不存在"
        lib = json.loads(lib_path.read_text(encoding="utf-8"))
        target = None
        for src in lib.get("sources", []):
            for cat in src.get("categories", []):
                for st in cat.get("strategies", []):
                    if st.get("id") == library_id:
                        target = (src, cat, st)
                        break
                if target:
                    break
            if target:
                break
        if not target:
            return f"❌ 策略大全中未找到 {library_id}"
        src, cat, st = target
        if st.get("implemented"):
            return f"⚠️ {library_id} 已是已实现状态: {st.get('desc', '')}"
        # 标记为已实现
        st["implemented"] = True
        st["desc"] = f"{st.get('desc', '')}(通过飞书Bot引入)"
        lib_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2), encoding="utf-8")
        return (
            f"✅ 策略 {library_id} 已标记为已实现\n"
            f"来源: {src['name']} / {cat['name']}\n"
            f"说明: {st.get('desc', '')}\n"
            f"注意: 标记为已实现只是元数据更新,实际量化逻辑需另外开发 strategy_xxx 函数才能用于 analyze"
        )
    except Exception as e:
        return f"❌ 引入策略出错: {e}"


def handler_backtest_strategy(strategy_id: str, sample: int = 0) -> str:
    """对指定策略做全市场回测。"""
    try:
        import backtest_builtin as bb
        log.info("开始回测策略 %s, sample=%d (耗时约 1-2 分钟)", strategy_id, sample)
        report = bb.run_backtest(limit=0, workers=1, sample=sample)
        strategies = report.get("strategies", {})
        # strategies 是 dict: {sid: {id, name, signal_count, horizons: {"5": {mean_ret, excess...}}}}
        if isinstance(strategies, dict):
            target = strategies.get(strategy_id)
        else:
            target = next((s for s in strategies if s.get("id") == strategy_id), None)
        if not target:
            available = list(strategies.keys()) if isinstance(strategies, dict) else [s["id"] for s in strategies]
            return f"❌ 未找到策略 {strategy_id},可用策略: {', '.join(available)}"
        h = target.get("horizons", {})
        baseline = report.get("baseline", {})
        # 生成回测图
        try:
            from feishu_image import gen_backtest_chart
            img = gen_backtest_chart(strategy_id)
            if img:
                _pending_images.append(img.getvalue())
        except Exception as e:
            log.warning("生成回测图失败: %s", e)
        out = (
            f"📊 **策略 {target.get('name', strategy_id)} 回测结果**\n"
            f"- 触发次数: {target.get('signal_count', 0)}\n"
            f"- 5天持有: 收益 {h.get('5', {}).get('mean_ret', 0)*100:+.2f}% / 超额 {h.get('5', {}).get('excess', 0)*100:+.2f}%\n"
            f"- 20天持有: 收益 {h.get('20', {}).get('mean_ret', 0)*100:+.2f}% / 超额 {h.get('20', {}).get('excess', 0)*100:+.2f}%\n"
            f"- 60天持有: 收益 {h.get('60', {}).get('mean_ret', 0)*100:+.2f}% / 超额 {h.get('60', {}).get('excess', 0)*100:+.2f}%\n"
            f"- 基准(全市场60天): {baseline.get('60', 0)*100:+.2f}%"
        )
        if _pending_images:
            out += "\n[已附回测收益曲线图]"
        return out
    except Exception as e:
        return f"❌ 回测出错: {e}"


def handler_grid_search(strategy_id: str, sample: int = 400) -> str:
    """对指定策略做参数网格寻优。"""
    try:
        import backtest_builtin as bb
        if strategy_id not in ("macd", "kdj", "boll", "dmi"):
            return f"❌ 网格寻优仅支持 macd/kdj/boll/dmi 四策略,不支持 {strategy_id}"
        log.info("开始网格寻优策略 %s, sample=%d (耗时约 2-5 分钟)", strategy_id, sample)
        report = bb.grid_search(sample=sample, horizon=20, workers=1)
        strategies = report.get("strategies", {})
        target = strategies.get(strategy_id) if isinstance(strategies, dict) else None
        if not target:
            return f"❌ 寻优完成但未找到策略 {strategy_id}"
        # grid 结构: {sid: {configs, sensitivity, default_excess, best_params, best_excess, ...}}
        lines = [f"🔍 **策略 {strategy_id} 网格寻优结果**"]
        if "default_excess" in target:
            lines.append(f"默认参数超额: {target['default_excess']*100:+.2f}%")
        if "best_params" in target:
            lines.append(f"最优参数: {target['best_params']}")
        if "best_excess" in target:
            lines.append(f"最优超额: {target['best_excess']*100:+.2f}%")
        sens = target.get("sensitivity", []) or target.get("configs", [])
        if sens:
            lines.append("\n参数敏感性(按超额排序 Top5):")
            for r in sens[:5]:
                params = r.get("params", {})
                excess = r.get("excess", 0)
                lines.append(f"  - {params}: {excess*100:+.2f}%")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 寻优出错: {e}"


# 工具名 → 处理函数映射
TOOL_HANDLERS = {
    "analyze_stock": lambda args: handler_analyze(args.get("code", "")),
    "get_market_status": lambda args: handler_market(),
    "get_yujie_picks": lambda args: handler_yujie(
        args.get("min_score", 0), args.get("hit_rule", "")
    ),
    "get_portfolio": lambda args: handler_portfolio(),
    # 策略管理 skill
    "list_strategies": lambda args: handler_list_strategies(),
    "get_strategy_library": lambda args: handler_get_strategy_library(
        source=args.get("source", ""),
        category=args.get("category", ""),
        implemented_only=args.get("implemented_only"),
        include_meta=bool(args.get("include_meta", False)),
        cross_ref=args.get("cross_ref", ""),
    ),
    "get_yujie_detail": lambda args: handler_get_yujie_detail(),
    "analyze_with_strategy": lambda args: handler_analyze_with_strategy(
        args.get("code", ""), args.get("strategy_id", "")
    ),
    "analyze_with_yujie": lambda args: handler_analyze_with_yujie(args.get("code", "")),
    "toggle_strategy": lambda args: handler_toggle_strategy(
        args.get("strategy_id", ""), bool(args.get("enabled", True))
    ),
    "set_strategy_params": lambda args: handler_set_strategy_params(
        args.get("strategy_id", ""), args.get("params", {})
    ),
    "enable_library_strategy": lambda args: handler_enable_library_strategy(args.get("library_id", "")),
    # 回测/寻优 skill
    "backtest_strategy": lambda args: handler_backtest_strategy(
        args.get("strategy_id", ""), int(args.get("sample", 0))
    ),
    "grid_search_strategy": lambda args: handler_grid_search(
        args.get("strategy_id", ""), int(args.get("sample", 400))
    ),
}

MAX_AGENT_STEPS = 6  # 最多 6 步推理(避免无限循环)

# 耗时工具(超过 10 秒),需先发"思考中"提示用户
SLOW_TOOLS = {"backtest_strategy", "grid_search_strategy"}

SYSTEM_PROMPT = """你是 A 股量化分析助手(集成于飞书群聊),拥有以下工具:

【数据查询类】
- analyze_stock(code): 个股技术面分析,返回已启用策略的买卖信号 + K线图
- get_market_status(): 今日市场概况(涨跌停/成交额)
- get_yujie_picks(min_score?, hit_rule?): 今日玉姐精选候选股(默认 Top10 + 缩略图墙)
  · min_score: 最低评分门槛,如 7=只看7+分强势股,5=玉姐默认门槛
  · hit_rule: 按命中规则过滤,如 "MACD金叉"/"突破+金叉"/"深回撤"
- get_portfolio(): 当前模拟盘持仓

【策略管理类】
- list_strategies(): 列出所有策略(23内置+自定义),含开关/参数/回测超额
- get_strategy_library(source?, category?, implemented_only?, include_meta?, cross_ref?): 多维度查策略大全
  · source: 来源过滤(book_cartoon=漫画书、book_caolian=操练大全、yujie_custom=玉姐、ai_custom=AI)
  · category: 章节/分类名模糊匹配(如"第15章"/"抄底")
  · implemented_only: true=只看已实现, false=只看未实现, 不传=全部
  · include_meta: true=附带书的作者/简介/章节列表
  · cross_ref: 跨来源对比,传策略id(如macd)查它在哪些书里出现(优先级最高)
- get_yujie_detail(): 查玉姐精选10条评分规则+权重+回测表现
- analyze_with_strategy(code, strategy_id): 用指定策略分析个股,联动策略大全给出"来源+核心逻辑+当前信号+理由" + K线图
  · strategy_id 支持引擎id(macd/kdj/boll)、大全id(macd_8/bottom)、中文名(抄底/逃顶)模糊匹配
  · 若策略已实现: 跑引擎分析,返回完整来源+逻辑+信号
  · 若策略未实现: 告知尚未实现,建议相近的已实现策略
- analyze_with_yujie(code): 用玉姐精选10条评分规则分析个股,给出综合评分+命中规则+解读 + 玉姐专属图
  · 玉姐是复合评分(10条规则累加),不是单策略买卖信号,需用此独立工具
  · 返回评分(满分12)、命中规则(带分值+说明)、未命中规则、评分解读
  · 附玉姐专属图: K线+评分命中标注(MACD金叉/绿柱缩短等箭头标注)
- toggle_strategy(strategy_id, enabled): 开关策略(操作类,需用户明确意图)
- set_strategy_params(strategy_id, params): 调策略参数(操作类,需用户明确意图)
- enable_library_strategy(library_id): 从大全引入策略(操作类)

【回测寻优类(耗时1-5分钟)】
- backtest_strategy(strategy_id, sample?): 全市场回测某策略,返回超额alpha
- grid_search_strategy(strategy_id, sample?): 参数网格寻优(仅macd/kdj/boll/dmi)

工作流程:
1. 根据用户问题决定调用哪个工具(可多次调用、组合调用)
2. 拿到工具返回的原始数据后,用简洁的中文向用户解释
3. 回复要条理清晰,用 markdown 格式(加粗、列表、emoji),不超过 600 字
4. 不要编造数据,只基于工具返回的事实
5. 涉及投资判断时务必加风险提示(非投资建议)

跨轮上下文(重要):
- 系统会保留最近 6 轮对话历史(user+assistant),用户问题可能承接上文
- 如用户先问"玉姐前10",再问"11-20呢" → 第二问指代玉姐精选的11-20名
- 如用户先问"分析茅台",再问"五粮液呢" → 第二问指代用同样方法分析五粮液
- 如用户先问"MACD策略",再问"KDJ呢" → 第二问指代看KDJ策略
- 收到指代不明的简短问题(如"11-20呢"/"X呢"/"换一个"),请结合历史理解,不要要求用户重述
- 用户说"重置"/"新话题"/"忘了吧"/"清空"等会被系统自动识别并清空历史,你无需处理

操作类工具授权原则:
- 用户明确表达意图(如"关闭均线组合策略"、"把BOLL周期改成30")才调用
- 模糊请求(如"看看策略")只调查询类
- 操作前可在回复中说明将要做什么,但用户已明确就直接执行

回测/寻优类:
- 耗时较长,用户问时主动提示"需要1-2分钟"
- 默认 sample=0(全市场),若用户说"快速测一下"可改 sample=200

策略大全查询模式识别:
- "漫画书/操练大全/玉姐/AI" → source 过滤
- "第X章/抄底/逃顶/选股" → category 模糊匹配
- "已实现/未实现/还有什么没做" → implemented_only 过滤
- "这本书讲了什么/简介/作者" → include_meta=true
- "MACD在哪些书里/X策略在哪些来源出现" → cross_ref
- "玉姐怎么打分/玉姐评分规则" → get_yujie_detail()

玉姐精选查询模式:
- "玉姐推荐什么/今日玉姐精选" → get_yujie_picks() → Top10 + 缩略图墙
- "玉姐7分以上的股票" → get_yujie_picks(min_score=7) → 强势股过滤
- "玉姐里命中MACD金叉的" → get_yujie_picks(hit_rule="MACD金叉") → 规则过滤
- "玉姐里的赤天化怎么样" → analyze_with_yujie(code=600227) → 评分+命中+玉姐专属图
- "用玉姐策略看茅台" → analyze_with_yujie(code=600519) → 评分+命中+玉姐专属图

示例:
- "分析茅台" → analyze_stock(code=600519) → 整理结果回复 + K线图
- "今天怎样" → get_market_status() → 总结市场情绪
- "玉姐推荐什么" → get_yujie_picks() → 列出 Top5 并点评 + 缩略图墙
- "玉姐7分以上的" → get_yujie_picks(min_score=7) → 强势股列表 + 图
- "持仓怎样" → get_portfolio() → 列出持仓并点评
- "有哪些策略" → list_strategies() → 整理表格回复
- "策略大全里还有什么" → get_strategy_library(implemented_only=false) → 列出未实现策略
- "漫画书那本讲了什么" → get_strategy_library(source=book_cartoon, include_meta=true)
- "操练大全第15章抄底策略有哪些" → get_strategy_library(source=book_caolian, category="第15章")
- "漫画书里哪些没实现" → get_strategy_library(source=book_cartoon, implemented_only=false)
- "MACD在哪些书里出现" → get_strategy_library(cross_ref="macd")
- "玉姐怎么打分" → get_yujie_detail()
- "看下茅台的MACD信号" → analyze_with_strategy(code=600519, strategy_id=macd) → 来源+逻辑+信号+K线图
- "用抄底策略分析茅台" → analyze_with_strategy(code=600519, strategy_id=bottom) → 来源+逻辑+信号+K线图
- "操练大全的MACD怎么样测茅台" → analyze_with_strategy(code=600519, strategy_id=macd_8) → 自动映射到 macd 引擎
- "用玉姐策略分析茅台" → analyze_with_yujie(code=600519) → 评分+命中规则+玉姐专属图
- "玉姐评分看下五粮液" → analyze_with_yujie(code=000858) → 评分+命中规则+玉姐专属图
- "玉姐里的赤天化怎么样" → analyze_with_yujie(code=600227) → 评分+命中规则+玉姐专属图
- "关闭均线组合策略" → toggle_strategy(strategy_id=ma_combo, enabled=false)
- "把BOLL周期改成30" → set_strategy_params(strategy_id=boll, params={period:30})
- "回测MACD策略" → backtest_strategy(strategy_id=macd) → 整理回测数据
- "寻优BOLL" → grid_search_strategy(strategy_id=boll) → 列出最优参数
"""


class FeishuAgent:
    """Function Calling Agent: LLM 自主决策调用工具,多步推理。"""

    def __init__(self):
        import os

        from ai_decider import load_env
        load_env()
        self.api_key = os.environ.get("AI_API_KEY", "")
        self.base_url = os.environ.get("AI_BASE_URL", "")
        self.model = os.environ.get("AI_MODEL", "")
        if not self.api_key or not self.base_url:
            raise RuntimeError("AI_API_KEY/AI_BASE_URL 未配置(检查 .env)")

    def chat(self, user_text: str, history: list | None = None) -> tuple[str, list, list[bytes]]:
        """Agent 主循环: ReAct(Reason→Act→Observe)直到模型给出最终答案。

        Args:
            user_text: 用户输入
            history: 之前的对话历史(用于多轮)
        Returns:
            (reply_text, new_history, images)  images 是 PNG bytes 列表
        """
        import httpx

        # 每轮对话前清空图片队列
        _pending_images.clear()

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        new_history = (history or []) + [{"role": "user", "content": user_text}]
        tool_log = []

        for step in range(MAX_AGENT_STEPS):
            payload = {
                "model": self.model,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.3,
                "max_tokens": 2048,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            try:
                with httpx.Client(timeout=60, trust_env=False) as c:
                    r = c.post(self.base_url, json=payload, headers=headers)
                if r.status_code != 200:
                    return f"❌ AI 调用失败(HTTP {r.status_code}): {r.text[:200]}", new_history, list(_pending_images)
                msg = r.json()["choices"][0]["message"]
            except Exception as e:
                log.error("Agent LLM 调用失败: %s", e)
                return f"❌ AI 调用异常: {e}", new_history, list(_pending_images)

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # 模型给出最终答案
                content = (msg.get("content") or "").strip()
                # 去除思考过程
                content = re.split(r"\n\s*(?:Thinking\s*Process|推理过程)[:：]", content)[0].strip()
                if tool_log:
                    log.info("Agent 完成, 共 %d 步, 工具调用: %s", step + 1, tool_log)
                new_history.append({"role": "assistant", "content": content})
                return content, new_history, list(_pending_images)

            # 有工具调用: 执行并把结果回灌
            # 注意: assistant 消息需保留 tool_calls 字段,OpenAI 规范要求
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                log.info("Agent step %d 调用 %s(%s)", step + 1, fn_name, fn_args)
                tool_log.append(fn_name)

                handler = TOOL_HANDLERS.get(fn_name)
                if handler is None:
                    result = f"错误: 未知工具 {fn_name}"
                else:
                    try:
                        result = handler(fn_args)
                    except Exception as e:
                        result = f"工具执行异常: {e}"
                        log.error("工具 %s 执行异常: %s", fn_name, e)

                # 工具结果回灌(messages 用 role=tool)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # 达到最大步数仍未给出最终答案
        log.warning("Agent 达到最大步数 %d,工具调用: %s", MAX_AGENT_STEPS, tool_log)
        return f"(推理步数已达上限,工具调用: {' → '.join(tool_log)}。请重新提问或换种问法。)", new_history, list(_pending_images)


# ============ 飞书长连接客户端 ============


class FeishuBotClient:
    """飞书长连接机器人客户端。"""

    def __init__(self):
        cfg = load_config().get("feishu", {})
        self.app_id = cfg.get("app_id", "")
        self.app_secret = cfg.get("app_secret", "")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("feishu app_id/app_secret 未配置")
        self.client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        log.info("飞书 Bot 客户端已初始化, app_id=%s...", self.app_id[:10])

    def _needs_thinking_hint(self, text: str) -> bool:
        """轻量判断:用户问题是否触发了耗时工具(回测/寻优)。

        用关键词匹配,避免额外 LLM 调用。
        """
        slow_keywords = ("回测", "测试", "寻优", "调参", "网格", "最优参数", "backtest", "grid")
        return any(kw in text.lower() for kw in slow_keywords)

    def _reply_text(self, chat_id: str, text: str):
        """发送文本消息到 chat_id。超长自动分段(按段落边界拆分)。"""
        # 飞书文本消息长度上限约 4096,超过则按段落边界拆分多条发送
        if len(text) <= 4000:
            self._send_text(chat_id, text)
            return

        # 按段落(\n\n)拆分,尽量不破坏 markdown 结构
        chunks = self._split_long_text(text, max_len=3800)
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk = f"(第{i}/{len(chunks)}段)\n\n{chunk}" if i > 1 else chunk
            self._send_text(chat_id, chunk)
        log.info("长文本拆分: %d 字 → %d 段", len(text), len(chunks))

    @staticmethod
    def _split_long_text(text: str, max_len: int = 3800) -> list[str]:
        """按段落边界拆分长文本,尽量在 \n\n 处断开。"""
        if len(text) <= max_len:
            return [text]
        chunks = []
        # 先按段落分
        paragraphs = text.split("\n\n")
        cur = ""
        for p in paragraphs:
            if len(cur) + len(p) + 2 <= max_len:
                cur = (cur + "\n\n" + p) if cur else p
            else:
                if cur:
                    chunks.append(cur)
                # 单段就超长,硬切
                while len(p) > max_len:
                    chunks.append(p[:max_len])
                    p = p[max_len:]
                cur = p
        if cur:
            chunks.append(cur)
        return chunks

    def _send_text(self, chat_id: str, text: str):
        """实际发送单条文本消息。"""
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("回复失败 code=%s msg=%s", resp.code, resp.msg)
        else:
            log.info("回复成功 message_id=%s", resp.data.message_id)

    def _reply_image(self, chat_id: str, png_bytes: bytes):
        """发送图片消息到 chat_id。"""
        from feishu import FeishuBot
        bot = FeishuBot()
        if not bot.enabled:
            log.info("feishu 未启用,跳过图片")
            return
        resp = bot.send_image(png_bytes, chat_id=chat_id)
        if resp and resp.get("code") == 0:
            log.info("图片回复成功 message_id=%s", resp.get("data", {}).get("message_id"))
        else:
            log.error("图片回复失败: %s", resp)

    def _handle_message(self, data: P2ImMessageReceiveV1) -> None:
        """处理收到的消息事件。"""
        try:
            msg = data.event.message
            chat_id = msg.chat_id
            msg_type = msg.message_type
            content_str = msg.content
            sender = data.event.sender.sender_id.open_id

            # 仅处理文本消息
            if msg_type != "text":
                self._reply_text(chat_id, "目前仅支持文本提问,例如:\n- 分析 600519\n- 市场\n- 玉姐\n- 持仓")
                return

            # 解析消息内容
            try:
                content = json.loads(content_str)
                text = content.get("text", "").strip()
            except Exception:
                text = ""

            # 去掉 @机器人 的 mention tag (飞书文本里以 @_<user_id> 形式存在)
            text = re.sub(r"@_\w+\s*", "", text).strip()
            if not text:
                self._reply_text(chat_id, "请输入您的问题,例如:\n- 分析 600519\n- 市场\n- 玉姐\n- 持仓")
                return

            log.info("收到消息 chat=%s sender=%s text=%r", chat_id, sender, text[:100])

            # 跨轮记忆: 按 chat_id+sender 隔离,群里不同用户各自独立历史
            session_id = f"{chat_id}:{sender}"

            # 重置命令:用户想清空历史时直接短路,不走 Agent
            if _is_reset_command(text):
                _clear_history(session_id)
                self._reply_text(
                    chat_id,
                    "🧹 已清空对话历史,我们重新开始吧!\n"
                    "你可以直接问问题,例如:\n"
                    "- 分析 600519\n"
                    "- 玉姐推荐什么\n"
                    "- 用 MACD 策略看茅台",
                )
                return

            # Agent 处理(Function Calling ReAct),失败降级到关键词路由
            # 智能判断: 只有调用耗时工具(backtest/grid_search)才发"思考中"提示,
            # 快速回复(分析/查询)直接给答案,避免收到两条消息的体验问题
            try:
                agent = FeishuAgent()
                history = _load_history(session_id)
                # 先看 LLM 第一步是否要调慢工具:用轻量探测(同 LLM 但只取 tool_calls)
                need_thinking_hint = self._needs_thinking_hint(text)
                if need_thinking_hint:
                    self._reply_text(chat_id, "🤔 正在思考,需要 1-2 分钟(回测/寻优)...")
                reply, new_history, images = agent.chat(text, history=history)
                _save_history(session_id, new_history)
            except Exception as e:
                log.warning("Agent 异常 %s, 降级到关键词路由", e)
                _, reply = route(text)
                images = []

            log.info("回复长度 %d, 附图 %d 张", len(reply), len(images))
            self._reply_text(chat_id, reply)
            # 发送图片
            for png in images:
                self._reply_image(chat_id, png)
        except Exception as e:
            log.error("处理消息异常: %s\n%s", e, traceback.format_exc())
            try:
                self._reply_text(data.event.message.chat_id, f"❌ 处理消息出错: {e}")
            except Exception:
                pass

    def run(self):
        """启动长连接。"""
        from lark_oapi.ws.client import Client as WsClient

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .build()
        )
        ws_client = WsClient(
            app_id=self.app_id,
            app_secret=self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )
        log.info("启动飞书长连接,等待群里 @机器人 提问...")
        ws_client.start()


# ============ CLI ============


def main():
    ap = argparse.ArgumentParser(description="飞书长连接机器人")
    ap.add_argument("--once", metavar="TEXT", help="单次路由测试(不走长连接,关键词路由)")
    ap.add_argument("--agent", metavar="TEXT", help="Agent 单次测试(Function Calling)")
    args = ap.parse_args()

    if args.once:
        name, reply = route(args.once)
        print(f"[关键词路由: {name}]")
        print(reply)
        return

    if args.agent:
        agent = FeishuAgent()
        # CLI 模式也走跨轮记忆,用 session_id="cli",方便测试多轮对话
        history = _load_history("cli")
        reply, new_history, images = agent.chat(args.agent, history=history)
        _save_history("cli", new_history)
        print("[Agent 回复]")
        print(reply)
        if images:
            out = Path("/tmp/agent_chart.png")
            out.write_bytes(images[0])
            print(f"\n[附图已保存到 {out},共 {len(images)} 张]")
        return

    client = FeishuBotClient()
    # 启动时清理过期历史
    _purge_old_history()
    # 重连循环(异常自动重启)
    while True:
        try:
            client.run()
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C,退出")
            break
        except Exception as e:
            log.error("长连接异常: %s, 10s 后重启", e)
            time.sleep(10)


if __name__ == "__main__":
    main()
