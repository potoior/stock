"""飞书应用 Bot 长连接机器人: 在群里 @机器人 提问,机器人回复。

工作模式: Function Calling Agent (LLM 自主决策调用工具,多步推理)
  用户问题 → LLM 决策 → 调用工具(analyze/market/yujie/portfolio) →
  LLM 整理结果 → 回复用户(可多轮调用)

工具(由 LLM 自动选择调用):
  analyze_stock(code)  个股技术面分析(45策略信号)
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
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
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

# 结构化工具调用日志(JSONL),便于审计/统计(OpenClaw 风格)
TOOL_AUDIT_LOG = Path("/tmp/feishu_bot_audit.jsonl")


def _log_tool_call(session_id: str, step: int, fn_name: str, fn_args: dict,
                   result_size: int, duration_ms: int, error: str | None = None) -> None:
    """写一条工具调用的结构化日志(JSONL)。失败不影响主流程。"""
    try:
        import os
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "session_id": session_id,
            "step": step,
            "tool": fn_name,
            "args": fn_args,
            "result_size": result_size,
            "duration_ms": duration_ms,
            "error": error,
        }
        with open(TOOL_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计日志失败不影响主流程
    # 同步累加运行状态
    _incr_stats("tool_calls", 1)
    if error:
        _incr_stats("tool_failures", 1)
    _stats_add_session(session_id)


# Bot 运行状态统计(进程级,线程安全)
_STATS: dict = {
    "start_time": datetime.now(),
    "llm_calls": 0,
    "llm_total_ms": 0,
    "llm_failures": 0,
    "tool_calls": 0,
    "tool_failures": 0,
    "sessions": set(),
}
_STATS_LOCK = threading.Lock()
MAX_TRACKED_SESSIONS = 500  # 运行状态里统计到的唯一会话数上限,防无界增长


def _incr_stats(key: str, delta: int = 1) -> None:
    """累加统计计数器(线程安全)。"""
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + delta


def _stats_add_session(session_id: str) -> None:
    """记录会话 ID(去重统计,带上限防无界增长)。"""
    with _STATS_LOCK:
        s = _STATS["sessions"]
        if session_id in s:
            return
        if len(s) >= MAX_TRACKED_SESSIONS:
            return  # 已达上限,仅作为统计展示,饱和即可
        s.add(session_id)


def _print_stats() -> None:
    """打印 Bot 运行状态摘要(启动时 + SIGUSR1 信号触发)。"""
    with _STATS_LOCK:
        s = dict(_STATS)
    uptime = datetime.now() - s["start_time"]
    hours = uptime.total_seconds() / 3600
    llm_avg = s["llm_total_ms"] / s["llm_calls"] if s["llm_calls"] else 0
    tool_err_rate = s["tool_failures"] / s["tool_calls"] * 100 if s["tool_calls"] else 0
    llm_err_rate = s["llm_failures"] / s["llm_calls"] * 100 if s["llm_calls"] else 0
    log.info(
        "Bot 运行状态 | 启动 %s | 在线 %.1fh | LLM 调用 %d 次(平均 %.0fms,失败 %d=%.1f%%) | "
        "工具调用 %d 次(失败 %d=%.1f%%) | 累计会话 %d 个",
        s["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
        hours, s["llm_calls"], llm_avg, s["llm_failures"], llm_err_rate,
        s["tool_calls"], s["tool_failures"], tool_err_rate,
        len(s["sessions"]),
    )


def _signal_handler_signusr1(signum, frame):
    """SIGUSR1 信号: 打印运行状态(journalctl 可见)。"""
    _print_stats()


def _register_stats_signal() -> None:
    """注册 SIGUSR1 信号(仅 main 线程可注册,失败静默)。"""
    import signal
    try:
        signal.signal(signal.SIGUSR1, _signal_handler_signusr1)
        log.info("已注册 SIGUSR1: kill -USR1 <pid> 可触发运行状态打印")
    except (ValueError, OSError):
        pass  # 非 main 线程或 Windows,跳过

# 命令路由关键字
CMD_ANALYZE = ("分析", "看看", "看看股")
CMD_MARKET = ("市场", "大盘", "行情", "今日")
CMD_YUJIE = ("玉姐", "候选", "精选", "top")
CMD_PORTFOLIO = ("持仓", "portfolio", "仓位", "股票池")

# ---- 会话级 thread-local 状态 ----
# 关键: 飞书 Bot 会并发处理不同 session 的消息(会话锁按 session_id 隔离,
# 同 session 串行、不同 session 并行)。因此累积图片队列和当前 session_id
# 必须是线程隔离的,否则并发下会串号/互相清空。
_tl = threading.local()


class _PendingImages:
    """thread-local 图片队列,行为兼容 list(append/clear/len/bool/迭代)。"""

    def _get(self) -> list:
        if not hasattr(_tl, "images"):
            _tl.images = []
        return _tl.images

    def append(self, val: bytes) -> None:
        self._get().append(val)

    def clear(self) -> None:
        self._get().clear()

    def __len__(self) -> int:
        return len(self._get())

    def __bool__(self) -> bool:
        return bool(self._get())

    def __iter__(self):
        return iter(self._get())


_pending_images = _PendingImages()


def _current_session_id() -> str:
    """读取当前线程的 session_id(默认 'cli')。"""
    return getattr(_tl, "session_id", "cli")


def _set_current_session_id(session_id: str) -> None:
    """设置当前线程的 session_id。"""
    _tl.session_id = session_id


def _current_chat_id() -> str:
    """读取当前线程的飞书 chat_id(默认 '')。"""
    return getattr(_tl, "chat_id", "")


def _set_current_chat_id(chat_id: str) -> None:
    """设置当前线程的 chat_id,供 handler 内部主动发消息(如进度提示)。"""
    _tl.chat_id = chat_id


def _current_chat_type() -> str:
    """读取当前线程的飞书 chat_type('p2p' 私聊 / 'group' 群聊,默认 'group')。"""
    return getattr(_tl, "chat_type", "group")


def _set_current_chat_type(chat_type: str) -> None:
    """设置当前线程的 chat_type,供 handler 判断群共享功能是否适用。"""
    _tl.chat_type = chat_type or "group"


# 当前线程的 FeishuBot 实例(供 handler 内部主动发消息)
_bot_ref = None


def _current_bot():
    """返回当前 FeishuBot 单例(若已初始化)。"""
    return _bot_ref


def _set_current_bot(bot):
    """设置 FeishuBot 单例(由 FeishuBot.__init__ 调用)。"""
    global _bot_ref
    _bot_ref = bot

# 跨轮对话历史持久化(按 session_id 存储,sqlite)
# 保留最近 MAX_HISTORY_TURNS 轮(1 轮 = user + assistant 两条消息)
# 超过 HISTORY_EXPIRE_DAYS 天未活跃的 session 自动清理(启动时跑一次)
HISTORY_DB = ENGINE_HOME / "agent_history.db"
MAX_HISTORY_TURNS = 8
HISTORY_EXPIRE_DAYS = 7

# 进程级 LRU 缓存: 最近 N 个 session 的 history(读命中跳过 sqlite)
# 配合 _save_history 写时同步更新缓存,高频对话省 sqlite 读
_HISTORY_CACHE: OrderedDict[str, list] = OrderedDict()
_HISTORY_CACHE_MAX = 32
_HISTORY_CACHE_LOCK = threading.Lock()  # 不同 session 并发访问需保护 OrderedDict


def _load_history(session_id: str) -> list:
    """从 LRU 缓存或 sqlite 加载会话历史。

    命中缓存时跳过 sqlite 读(高频对话加速),未命中读 sqlite 并入缓存。
    """
    # 1. 先查 LRU 缓存
    with _HISTORY_CACHE_LOCK:
        cached = _HISTORY_CACHE.get(session_id)
        if cached is not None:
            _HISTORY_CACHE.move_to_end(session_id)
            return cached
    # 2. 未命中,读 sqlite
    try:
        conn = _history_db()
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
            history = json.loads(row[0])
            _put_history_cache(session_id, history)
            return history
    except Exception as e:
        log.warning("加载历史失败 %s: %s", session_id, e)
    return []


def _put_history_cache(session_id: str, history: list) -> None:
    """写入 LRU 缓存(超限时淘汰最久未访问的)。"""
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE[session_id] = history
        _HISTORY_CACHE.move_to_end(session_id)
        while len(_HISTORY_CACHE) > _HISTORY_CACHE_MAX:
            _HISTORY_CACHE.popitem(last=False)


def _invalidate_history_cache(session_id: str) -> None:
    """清除某 session 的缓存(重置/清空时调用)。"""
    with _HISTORY_CACHE_LOCK:
        _HISTORY_CACHE.pop(session_id, None)


# 历史里 assistant 消息裁剪阈值(超长截断到摘要,避免回测/策略大全等长回复撑爆历史)
HISTORY_MSG_MAX_CHARS = 500
HISTORY_MSG_KEEP_CHARS = 200

# 历史压缩(Compaction, OpenClaw 风格):历史达到 N 条时,把最旧的几轮用 LLM 总结成 1 条
# 触发阈值: 16 条(8 轮);压缩后: 1 条摘要 + 最近 12 条(6 轮)原文
# 注: 阈值/保留比例影响压缩频率 —— 旧参数(10/8)压缩后 1 轮即再触发,
# 每轮多烧一次 LLM 调用;16/12 压缩后需 2 轮才再触发,省一半压缩调用
COMPACTION_THRESHOLD = 16
COMPACTION_KEEP_RECENT = 12


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
        conn = _history_db()
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
        # 同步更新 LRU 缓存(下次 _load_history 命中跳过 sqlite)
        _put_history_cache(session_id, history)
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
        conn = _history_db()
        conn.execute("DELETE FROM agent_history WHERE session_id=?", (session_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("清空历史失败 %s: %s", session_id, e)
    _invalidate_history_cache(session_id)


def _purge_old_history() -> int:
    """清理超过 HISTORY_EXPIRE_DAYS 天未活跃的会话历史。

    Bot 启动时调用一次,防止 sqlite 长期积累无用 session。
    返回清理的条数。
    """
    try:
        conn = _history_db()
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


# ============ 自选股 ============

WATCHLIST_DB = ENGINE_HOME / "agent_watchlist.db"


def _watchlist_db():
    """自选股 sqlite,按 session_id(用户)隔离。启用 WAL 防群内并发锁竞争。"""
    conn = sqlite3.connect(str(WATCHLIST_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            session_id TEXT,
            code TEXT,
            name TEXT,
            ts INTEGER,
            PRIMARY KEY (session_id, code)
        )
    """)
    conn.commit()
    return conn


def _history_db():
    """对话历史 sqlite 连接(启用 WAL,防高频对话锁竞争)。"""
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def watchlist_add(session_id: str, code: str, name: str = "") -> str:
    """添加自选股。"""
    try:
        conn = _watchlist_db()
        conn.execute(
            "INSERT OR REPLACE INTO watchlist(session_id, code, name, ts) VALUES(?,?,?,?)",
            (session_id, code, name, int(time.time())),
        )
        conn.commit()
        conn.close()
        return f"✅ 已添加 {code} {name} 到自选"
    except Exception as e:
        return f"❌ 添加自选失败: {e}"


def watchlist_remove(session_id: str, code: str) -> str:
    """删除自选股。"""
    try:
        conn = _watchlist_db()
        cur = conn.execute(
            "DELETE FROM watchlist WHERE session_id=? AND code=?",
            (session_id, code),
        )
        n = cur.rowcount
        conn.commit()
        conn.close()
        return f"✅ 已从自选移除 {code} (删了 {n} 条)" if n else f"⚠️ 自选中没有 {code}"
    except Exception as e:
        return f"❌ 删除自选失败: {e}"


def watchlist_list(session_id: str) -> list[dict]:
    """列出自选股,返回 [{code, name, ts}]。"""
    try:
        conn = _watchlist_db()
        rows = conn.execute(
            "SELECT code, name, ts FROM watchlist WHERE session_id=? ORDER BY ts",
            (session_id,),
        ).fetchall()
        conn.close()
        return [{"code": r[0], "name": r[1], "ts": r[2]} for r in rows]
    except Exception:
        return []


def watchlist_group_add(session_id: str, code: str, name: str = "") -> str:
    """添加到群共享自选池(以 chat_id 为 key,去 sender)。

    session_id 格式: "<chat_id>:<sender>",取 chat_id 部分作为群共享 key。
    群内任一成员添加的股票都对全群可见,实现"群友共同关注"。
    """
    chat_id = session_id.split(":", 1)[0] if ":" in session_id else session_id
    group_key = f"group:{chat_id}"
    return watchlist_add(group_key, code, name)


def watchlist_group_list(session_id: str) -> list[dict]:
    """列出群共享自选池(去重所有群成员添加的)。"""
    chat_id = session_id.split(":", 1)[0] if ":" in session_id else session_id
    group_key = f"group:{chat_id}"
    return watchlist_list(group_key)


def watchlist_group_remove(session_id: str, code: str) -> str:
    """从群共享自选池删除。"""
    chat_id = session_id.split(":", 1)[0] if ":" in session_id else session_id
    group_key = f"group:{chat_id}"
    return watchlist_remove(group_key, code)



# ============ 配置 ============


def load_config():
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


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
    # 用 stock_names.resolve_code(腾讯搜索全市场)替代本地退化版(17 硬编码)
    try:
        from stock_names import resolve_code as _resolve_code
    except ImportError:
        _resolve_code = lambda x: None  # noqa: E731
    code = _resolve_code(text)
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
        buys = r.get("buy_reasons", [])[:10]
        sells = r.get("sell_reasons", [])[:10]
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
            "运行 `python daily_scan.py` 生成,或等待 09:35 自动任务。\n"
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
                "运行 `python yujie_scan.py` 或等待 09:35 自动任务。"
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

        # 精简版:默认只列 Top5 详情,完整 10 只看附图
        text_show = show[:5] if not has_filter else show[:10]
        lines = [f"🎯 今日玉姐精选{cond_str} 共 {len(filtered)} 只(详情看附图)"]
        for p in text_show:
            hits = "、".join(p.get("hits", [])[:2]) if p.get("hits") else "无命中"
            if p.get("hits") and len(p["hits"]) > 2:
                hits += f"等{len(p['hits'])}条"
            lines.append(
                f"{p['rank']}. **{p['code']} {p['name']}** | {p['score']:g}分 | {hits}"
            )
        if len(filtered) > len(text_show):
            lines.append(f"\n(共 {len(filtered)} 只,文字仅列前 {len(text_show)} 只,完整 {len(show)} 只见附图)")
        if _pending_images:
            lines.append("[已附候选 K 线缩略图墙]")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 读取玉姐精选出错: {e}"


def handler_watchlist(action: str, codes: list = None, session_id: str = "cli") -> str:
    """自选股管理:add/remove/list/analyze,按 session_id 隔离(每人独立)。

    codes 里的元素可以是代码或名称,统一解析成 6 位代码 + 名称。
    analyze: 批量分析自选股(调 handler_compare_stocks 做 PE/PB/ROE 对比)
    """
    if action == "list":
        items = watchlist_list(session_id)
        if not items:
            return "📭 你的自选股列表为空。\n用 \"加自选 茅台\" 或 \"加自选 600519\" 添加。"
        lines = [f"📌 你的自选股({len(items)} 只)"]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['code']} {it['name'] or '-'}")
        return "\n".join(lines)

    if action in ("group_list", "group_analyze"):
        # 群共享功能仅群聊可用,1v1 私聊 chat_type=p2p
        if _current_chat_type() == "p2p":
            return "💡 群共享自选股功能仅在群聊中可用。\n私聊请用 '加自选'/'我的自选' 管理个人列表。"
        # 群共享自选池:所有群成员添加的去重列表
        items = watchlist_group_list(session_id)
        if not items:
            return "📭 群共享自选池为空。\n用 \"群加自选 茅台\" 添加(全群可见)。"
        if action == "group_list":
            lines = [f"👥 群共享自选池({len(items)} 只,全群可见)"]
            for i, it in enumerate(items, 1):
                lines.append(f"{i}. {it['code']} {it['name'] or '-'}")
            return "\n".join(lines)
        # group_analyze: 批量分析
        if len(items) == 1:
            return handler_analyze(items[0]["code"])
        codes = [it["code"] for it in items[:8]]
        extra = (f"\n\n(群共享共 {len(items)} 只,仅分析前 8。"
                 f"看其余: '分析群自选 9-16'") if len(items) > 8 else ""
        return handler_compare_stocks(codes) + extra

    if action == "analyze":
        # 批量分析自选股:调 handler_compare_stocks 做财务对比
        items = watchlist_list(session_id)
        if not items:
            return "📭 你的自选股列表为空,无法分析。\n用 \"加自选 茅台\" 添加后再试。"
        if len(items) == 1:
            # 单只直接 analyze_stock
            return handler_analyze(items[0]["code"])
        # 多只:compare_stocks 最多 8 只
        codes = [it["code"] for it in items[:8]]
        extra = f"\n\n(自选共 {len(items)} 只,仅分析前 8。看其余: '分析自选 9-16')" if len(items) > 8 else ""
        return handler_compare_stocks(codes) + extra

    if not codes:
        return "❌ add/remove/group_add/group_remove 操作需要 codes 参数,如 [\"600519\"] 或 [\"茅台\"]"

    # 解析每个 code/name 为 6 位代码
    try:
        from stock_names import resolve_code
    except ImportError:
        # 退化:直接当代码用
        resolve_code = lambda x: x if len(x) == 6 and x.isdigit() else None  # noqa: E731

    resolved = []  # [(code, name)]
    for c in codes:
        code = resolve_code(c)
        if not code:
            # 6 位代码直接用
            if len(c) == 6 and c.isdigit():
                code = c
            else:
                continue
        # 拿名称:用户输入是名称就用它,是代码就用空名(后续可拿实时名补)
        name = "" if c.isdigit() else c
        resolved.append((code, name))

    if not resolved:
        return f"❌ 未能识别任何股票: {codes}"

    # group_* 在 1v1 私聊禁用(语义错误)
    if action.startswith("group_") and _current_chat_type() == "p2p":
        return "💡 群共享自选股功能仅在群聊中可用。\n私聊请用 '加自选'/'删自选'/'我的自选' 管理个人列表。"

    if action in ("add", "group_add"):
        # 名称缺失时通过实时接口补名(批量查,1次网络)
        code_only = [c for c, n in resolved if not n]
        if code_only:
            try:
                import strategy_engine as se
                rt_list = se.fetch_realtime(code_only)
                rt_map = {r["code"]: r.get("name", "") for r in rt_list}
                resolved = [(c, n or rt_map.get(c, "")) for c, n in resolved]
            except Exception:
                pass
        msgs = []
        is_group = action == "group_add"
        for code, name in resolved:
            if is_group:
                msgs.append(watchlist_group_add(session_id, code, name))
            else:
                msgs.append(watchlist_add(session_id, code, name))
        if is_group:
            cnt = len(watchlist_group_list(session_id))
            return "\n".join(msgs) + f"\n\n群共享自选池共 {cnt} 只,发\"群自选\"查看(全群可见)"
        cnt = len(watchlist_list(session_id))
        return "\n".join(msgs) + f"\n\n当前自选 {cnt} 只,发\"我的自选\"查看"
    elif action in ("remove", "group_remove"):
        msgs = []
        is_group = action == "group_remove"
        for code, _ in resolved:
            if is_group:
                msgs.append(watchlist_group_remove(session_id, code))
            else:
                msgs.append(watchlist_remove(session_id, code))
        return "\n".join(msgs)
    else:
        return (f"❌ 未知 action: {action},应为 "
                f"add/remove/list/analyze 或 group_add/group_remove/group_list/group_analyze")


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


def handler_finance(code: str) -> str:
    """获取个股财务数据(PE/PB/市值/ROE/毛利率/净利率/营收/净利润/同比)。"""
    import stock_names
    from stock_finance import fetch_finance, fmt_finance

    # 支持中文简称:先用 stock_names 解析
    if code and not code.isdigit():
        codes = stock_names.resolve_codes(code)
        if not codes:
            return f"❌ 无法识别股票名 '{code}',请用 6 位代码或全名(如 '600519' 或 '茅台')"
        code = codes[0]
    if not code or not code.isdigit() or len(code) != 6:
        return f"❌ 代码格式错误: '{code}',需 6 位数字"

    data = fetch_finance(code)
    return fmt_finance(data)


def handler_compare_stocks(codes: list) -> str:
    """多股票对比: 一次给 N 只股票的对比表(PE/PB/ROE/市值/净利率)。

    Args:
        codes: 股票代码或名称列表,如 ["600519","000858"] 或 ["茅台","五粮液"]
    """
    import stock_names
    from stock_finance import fetch_finance

    if not codes or not isinstance(codes, list):
        return "❌ 请提供要对比的股票列表,如 ['茅台','五粮液']"
    if len(codes) > 8:
        return "❌ 最多对比 8 只股票,请精简列表"

    # 解析代码(支持中文简称)
    resolved = []
    for c in codes:
        c = (c or "").strip()
        if not c:
            continue
        if c.isdigit() and len(c) == 6:
            resolved.append(c)
        else:
            r = stock_names.resolve_codes(c)
            if r:
                resolved.append(r[0])
    if not resolved:
        return f"❌ 无法解析任何代码,输入: {codes}"
    if len(resolved) > 8:
        resolved = resolved[:8]

    # 拉数据(每只调 fetch_finance,有缓存不会慢)
    rows = []
    for code in resolved:
        d = fetch_finance(code)
        if "error" in d:
            rows.append(("-", code) + tuple(["-"] * 7))
            continue
        rows.append((
            d.get("name", "-"),
            code,
            f"{d['pe_ttm']:.2f}" if isinstance(d.get("pe_ttm"), (int, float)) else "-",
            f"{d['pb']:.2f}" if isinstance(d.get("pb"), (int, float)) else "-",
            f"{d['total_mv']/1e8:.0f}亿" if isinstance(d.get("total_mv"), (int, float)) else "-",
            f"{d['roe']:.2f}%" if isinstance(d.get("roe"), (int, float)) else "-",
            f"{d['net_margin']:.2f}%" if isinstance(d.get("net_margin"), (int, float)) else "-",
            d.get("report_name", "-"),
        ))

    # 表格输出
    lines = [
        f"### 📊 {len(resolved)} 只股票对比",
        "",
        "| 名称 | 代码 | PE(TTM) | PB | 总市值 | ROE | 净利率 | 财报 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    lines.append("")
    lines.append("💡 可继续追问单只股票详情(如'茅台基本面')")
    return "\n".join(lines)


# 主流板块成分股(简化版,涵盖 A 股常见板块,每板块 8 只代表股)
# 仅作为东财板块接口失败时的 fallback,正常情况下走动态查询
_SECTOR_MEMBERS = {
    "白酒": ["600519", "000858", "000568", "600809", "002304", "000596", "603369", "600779"],
    "银行": ["601398", "601939", "601288", "601318", "600036", "601166", "600000", "601628"],
    "医药": ["600276", "300015", "600436", "000538", "600196", "300003", "000999", "600085"],
    "新能源": ["300750", "002594", "601012", "600438", "002460", "603259", "300274", "002129"],
    "半导体": ["688981", "002049", "603501", "603160", "688012", "300661", "300223", "002405"],
    "消费": ["600887", "000651", "600690", "000333", "002508", "600061", "603288", "000858"],
    "军工": ["600760", "000768", "600031", "002179", "600150", "000901", "600893", "002049"],
    "地产": ["000002", "600048", "001914", "600340", "000671", "600208", "600383", "000961"],
    "电力": ["600900", "601016", "600795", "000875", "600025", "600674", "003816", "600027"],
    "有色": ["601899", "603993", "600547", "002460", "000831", "600362", "002203", "600497"],
}

# 东财行业板块列表缓存: {板块名: BK代码},进程级缓存,首次查询后不再请求
_SECTOR_INDEX_CACHE: dict[str, str] = {}
_SECTOR_INDEX_FAIL_TS: float = 0.0  # 上次失败时间,失败后 5 分钟内不再重试


def _fetch_sector_index() -> dict[str, str]:
    """拉取东财板块索引(m:90 t=2 概念 + t=3 行业,共 ~1000 个),返回 {板块名: BK代码}。

    失败返回 {}。结果进程级缓存,避免重复请求。
    东财单页最多 100 条,需分页拉取。
    失败后 5 分钟内不再重试,避免短时故障时每次调用都打 API。
    """
    global _SECTOR_INDEX_FAIL_TS
    if _SECTOR_INDEX_CACHE:
        return _SECTOR_INDEX_CACHE
    # 失败冷却:5 分钟内不重试
    if _SECTOR_INDEX_FAIL_TS and (time.time() - _SECTOR_INDEX_FAIL_TS) < 300:
        return {}
    import urllib.request
    try:
        for t in (2, 3):  # 2=概念板块, 3=行业板块
            for page in range(1, 10):  # 单类最多 9 页 900 个,足够覆盖
                url = (
                    f"http://17.push2.eastmoney.com/api/qt/clist/get"
                    f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:{t}&fields=f12,f14"
                )
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
                diff = (data.get("data") or {}).get("diff") or []
                if not diff:
                    break
                for r in diff:
                    name = r.get("f14")
                    code = r.get("f12")
                    if name and code and isinstance(name, str):
                        _SECTOR_INDEX_CACHE[name] = code
                if len(diff) < 100:
                    break
        log.info("东财板块索引加载 %d 个(概念+行业)", len(_SECTOR_INDEX_CACHE))
    except Exception as e:
        _SECTOR_INDEX_FAIL_TS = time.time()
        # 已拉到的部分数据保留在 _SECTOR_INDEX_CACHE 中,后续查询仍可用
        log.warning("东财板块列表获取部分失败,已缓存 %d 个: %s",
                    len(_SECTOR_INDEX_CACHE), e)
    return _SECTOR_INDEX_CACHE


def _fetch_sector_members(bk_code: str, top_n: int = 8) -> list[str]:
    """从东财板块接口取成分股(按成交额降序,取前 top_n 只 6 位代码)。

    Args:
        bk_code: BK 板块代码,如 "BK0896"(白酒)
        top_n: 取前 N 只(默认 8,与对比表上限一致)

    Returns: 6 位代码列表,失败返回 []
    """
    import urllib.request
    url = (
        f"http://17.push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz={top_n}&po=1&np=1&fltt=2&invt=2&fid=f6&fs=b:{bk_code}&fields=f12"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        out = []
        for r in (data.get("data") or {}).get("diff", []) or []:
            code = r.get("f12")
            if code and isinstance(code, str) and code.isdigit() and len(code) == 6:
                out.append(code)
        return out
    except Exception as e:
        log.warning("东财板块成分股获取失败 %s: %s", bk_code, e)
        return []


def handler_analyze_sector(sector: str) -> str:
    """板块分析: 给出板块成分股的对比表(PE/PB/ROE/市值/净利率)。

    优先用东财板块接口动态查成分股(覆盖全 A 股 ~500 个行业板块),
    失败 fallback 到 _SECTOR_MEMBERS 硬编码 10 个主流板块。

    Args:
        sector: 板块名(中文),如"白酒"/"银行"/"医药"/"新能源"/"半导体"
    """
    if not sector:
        return "❌ 请提供板块名,如'白酒'/'银行'/'医药'/'新能源'/'半导体'"

    sector = sector.strip()

    # 1. 优先用东财动态查询(板块名精确或模糊匹配)
    sectors = _fetch_sector_index()
    matched_bk = None
    matched_name = None
    if sectors:
        # 精确匹配优先
        if sector in sectors:
            matched_bk = sectors[sector]
            matched_name = sector
        else:
            # 模糊匹配: 找包含 sector 的板块名
            for name, bk in sectors.items():
                if sector in name or name in sector:
                    matched_bk = bk
                    matched_name = name
                    break

    if matched_bk:
        members = _fetch_sector_members(matched_bk, top_n=8)
        if members:
            return _format_sector_compare(matched_name, members)
        # 动态查询失败,继续 fallback

    # 2. Fallback: 硬编码 _SECTOR_MEMBERS
    matched = None
    for k in _SECTOR_MEMBERS:
        if sector == k or sector in k or k in sector:
            matched = k
            break
    if matched:
        return _format_sector_compare(matched, _SECTOR_MEMBERS[matched])

    # 都没匹配上,提示用户
    known_hard = "、".join(_SECTOR_MEMBERS.keys())
    hint = f"❌ 未识别板块 '{sector}'。已支持主流板块: {known_hard}"
    if sectors:
        # 给出几个东财也有的相似板块名作为提示
        similar = [n for n in sectors if sector[:2] in n][:5]
        if similar:
            hint += f"\n💡 东财板块中相似名: {', '.join(similar)}"
    return hint


def _format_sector_compare(sector_name: str, members: list) -> str:
    """板块成分股对比的统一输出格式。"""
    header = f"### 📊 板块【{sector_name}】成分股对比({len(members)} 只,按成交额排序)\n\n"
    body = handler_compare_stocks(members)
    # compare_stocks 内部已有标题"📊 N 只股票对比",这里替换为板块标题
    # body 第一行是 "### 📊 N 只股票对比",第二行空,第三行起是表头
    parts = body.split("\n", 2)
    if len(parts) == 3 and parts[0].startswith("### 📊"):
        return header + parts[2]
    return header + body


def _normalize_date(date_str: str) -> str:
    """规范化日期输入为 YYYYMMDD。
    支持: '20260819' / '2026-08-19' / '2026/08/19' / '昨天' / '前天' / '大前天'
    相对日期(昨天/前天)自动回退到最近交易日(跳过周六日),避免落在非交易日误报无数据。
    """
    if not date_str:
        return datetime.now().strftime("%Y%m%d")
    s = date_str.strip()
    # 相对日期
    if s in ("昨天", "昨日", "yesterday"):
        return _rollback_to_weekday(1).strftime("%Y%m%d")
    if s in ("前天",):
        return _rollback_to_weekday(2).strftime("%Y%m%d")
    if s in ("大前天",):
        return _rollback_to_weekday(3).strftime("%Y%m%d")
    if s in ("今天", "今日", "today"):
        return datetime.now().strftime("%Y%m%d")
    # 数字日期
    s = s.replace("-", "").replace("/", "").replace(".", "")
    if s.isdigit() and len(s) == 8:
        return s
    return ""  # 无效


def _rollback_to_weekday(days_back: int) -> datetime:
    """从今天往前推 N 个自然日,若落在周末则继续回退到周五。

    交易日历无法完整获知,只处理最常见的周末回退。
    weekday(): Mon=0 ... Sun=6,周六=5周日=6。
    """
    d = datetime.now() - timedelta(days=days_back)
    while d.weekday() >= 5:  # 周六(5)/周日(6)
        d -= timedelta(days=1)
    return d


def handler_query_history_picks(date: str) -> str:
    """查询过去某天的玉姐精选(历史复盘)。

    Args:
        date: 日期,支持 '20260819' / '2026-08-19' / '昨天' / '前天'
    """
    import yujie_scan

    date_str = _normalize_date(date)
    if not date_str:
        return (f"❌ 日期格式错误: '{date}'。"
                "支持 YYYYMMDD / YYYY-MM-DD / 昨天 / 前天 / 大前天")

    picks = yujie_scan.load_picks(date_str)
    if not picks:
        # 看看 db 里有哪些日期
        try:
            conn = sqlite3.connect(str(ENGINE_HOME / "stock_cache.db"), timeout=5)
            rows = conn.execute(
                "SELECT DISTINCT date FROM yujie_picks ORDER BY date DESC LIMIT 5"
            ).fetchall()
            conn.close()
            available = "、".join(r[0] for r in rows) if rows else "无"
        except Exception:
            available = "查询失败"
        return (
            f"❌ {date_str} 没有玉姐精选数据。\n"
            f"最近可查日期: {available}\n"
            "提示: 玉姐精选每日 09:35 自动生成,历史数据需当天跑过才有。"
        )

    # Top10 列表(精简: 默认只展开 top 5 详情,避免超 600 字截断)
    top = picks[:10]
    show_n = min(5, len(top))  # 展开前 5,与 handler_yujie 一致
    lines = [f"📅 {date_str} 玉姐精选(共 {len(picks)} 只,显示前 {show_n})"]
    for p in top[:show_n]:
        hits = "、".join(p.get("hits", [])[:3]) if p.get("hits") else "无命中"
        if p.get("hits") and len(p["hits"]) > 3:
            hits += f" 等{len(p['hits'])}项"
        lines.append(f"{p['rank']}. **{p['code']} {p['name']}** | {p['score']:g}分 | {hits}")

    # 6-10 只精简一行展示
    if len(top) > show_n:
        rest = "、".join(f"{p['code']}({p['score']:g})" for p in top[show_n:])
        lines.append(f"\n6-{len(top)}: {rest}")

    if len(picks) > 10:
        lines.append(f"(共 {len(picks)} 只,完整 10 只可追问 '分析 XXX')")

    # 评分分布
    scores = [p.get("score", 0) for p in picks]
    if scores:
        high = sum(1 for s in scores if s >= 7)
        mid = sum(1 for s in scores if 5 <= s < 7)
        low = sum(1 for s in scores if s < 5)
        lines.append(f"\n评分: 7+分 {high} / 5-7分 {mid} / <5分 {low}")

    lines.append(f"\n💡 可追问: '分析 {top[0]['code']}'")
    return "\n".join(lines)


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


# 工具 schema + SYSTEM_PROMPT 提取到 feishu_bot_tools.py(纯数据,减少主文件体积)
from feishu_bot_tools import SYSTEM_PROMPT, TOOLS  # noqa: E402,F401  (re-export 保持兼容)


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

        # 精简版:只列摘要 + Top5 超额,避免长表格(完整列表用 get_strategy_library 查)
        enabled_count = sum(1 for s in strategies if s.get("enabled", True))
        disabled = [s for s in strategies if not s.get("enabled", True)]
        # 按超额排序取 Top5
        sorted_by_excess = sorted(
            [(s.get("id", ""), s.get("name", ""), excess_map.get(s.get("id", ""), 0))
             for s in strategies if s.get("id", "") in excess_map],
            key=lambda x: x[2], reverse=True
        )[:5]

        lines = [
            f"📋 **当前策略状态** 共 {len(strategies)} 个(启用 {enabled_count} / 禁用 {len(disabled)})",
            "",
            "**Top5 60天超额**:",
        ]
        for sid, name, exc in sorted_by_excess:
            lines.append(f"- {name}({sid}): {exc:+.2f}%")
        if disabled:
            lines.append(f"\n**已禁用**({len(disabled)} 个): " + ", ".join(f"{s['id']}" for s in disabled[:5]))
            if len(disabled) > 5:
                lines.append(f"  …共 {len(disabled)} 个")
        lines.append("\n(完整策略列表用'策略大全'查,启停用'开关 策略ID')")
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
                if target in (sid, eid) or sid.startswith(target + "_") or eid == target:
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
            hint = ""
            if category:
                # 自愈提示: 列出全部合法分类,避免 LLM 连续盲猜浪费步数(9-01 事故:
                # LLM 猜了 8 个不存在的分类,耗尽 6 步上限)
                valid = []
                for src in lib.get("sources", []):
                    for cat in src.get("categories", []):
                        n = cat.get("name", "")
                        if n and n not in valid:
                            valid.append(n)
                hint = f"\n可用分类(子串匹配): {'/'.join(valid)}\n提示: 查策略用本工具;找股票请用 scan_with_strategy 或 scan_with_yujie。"
            return f"❌ 无匹配策略(过滤条件: {', '.join(filters) or '无'}){hint}"

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
                if target in (sid, eid) or sid.startswith(target + "_") or eid == target:
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

        # 5. 组装输出(精简版,防超 600 字截断)
        lines = [
            f"📊 {code} {name} {emoji} {price:.2f} ({pct:+.2f}%)",
            f"🎯 **玉姐评分: {score:g} 分** / 满分 {total_possible:g} 分",
        ]

        if hit_rules:
            lines.append(f"\n✅ **命中规则**({len(hit_rules)}条,{sum(r['score'] for r in hit_rules):g}分)")
            for r in hit_rules:
                lines.append(f"- {r['name']} +{r['score']}")

        if miss_rules:
            # 未命中规则只列名,不展开 desc,避免超 600 字
            miss_names = "、".join(r["name"] for r in miss_rules)
            lines.append(f"\n⚪ **未命中**({len(miss_rules)}条): {miss_names}")

        # 6. 评分解读
        if score >= 7:
            comment = f"🚀 **强势**({score:g}分),历史 60 天超额 +11.79%"
        elif score >= 5:
            comment = f"📊 **中等偏强**({score:g}分),玉姐精选通常 5+ 分入选"
        elif score >= 3:
            comment = f"⚠️ **偏弱**({score:g}分),低于 5 分入选门槛"
        else:
            comment = f"❌ **弱**({score:g}分),暂不符合玉姐精选标准"
        lines.append(f"\n💡 {comment}")

        if _pending_images:
            lines.append("\n[已附玉姐专属图: K线+评分标注]")
        lines.append("\n⚠️ 技术面评分,不构成投资建议")
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


def handler_combo_backtest(
    strategy_ids: list[str], mode: str = "and", horizon: int = 20, sample: int = 400
) -> str:
    """多策略组合回测(AND=同日同时触发, OR=任一触发)。"""
    try:
        import backtest_builtin as bb
        time_hint = "全市场约 5-10 分钟" if sample == 0 or sample >= 2000 else "约 1-3 分钟"
        log.info("开始组合回测 %s [%s], horizon=%d, sample=%d (%s)",
                 strategy_ids, mode, horizon, sample, time_hint)

        # 进度回调(同 scan_with_strategy / scan_with_yujie)
        chat_id = _current_chat_id()
        bot_ref = _current_bot()
        last_progress_ts = [0.0]

        def _progress_cb(scanned, total, hits_count):
            import time as _t
            now = _t.time()
            if now - last_progress_ts[0] < 30 and scanned != total:
                return
            last_progress_ts[0] = now
            if bot_ref and chat_id:
                pct = scanned * 100 // total if total else 0
                try:
                    bot_ref._send_text(
                        chat_id,
                        f"⏳ 组合回测预加载: {scanned}/{total} ({pct}%) | 有效 {hits_count} 只",
                    )
                except Exception:
                    pass

        report = bb.run_combo_backtest(
            strategy_ids, mode, horizon, sample, workers=1,
            progress_callback=_progress_cb if (bot_ref and chat_id) else None,
        )
        if "error" in report:
            return f"❌ {report['error']}"
        combo = report["combo"]
        per = report["per_strategy"]
        baseline = report["baseline"]

        mode_label = "同日同时触发(AND)" if mode == "and" else "任一触发(OR)"
        # 多策略(>=4)时用紧凑格式防超 600 字
        compact = len(strategy_ids) >= 4
        lines = [
            f"📊 **多策略组合回测** {' + '.join(strategy_ids)} [{mode_label}]",
            f"- 持有期: {report['horizon']} 天 | 抽样: {report['sample']} 只 | 基准 {baseline*100:+.2f}%",
            "",
            f"**组合信号** 触发 {combo['signal_count']} 次,收益 {combo['mean_ret']*100:+.2f}%,"
            f"超额 {combo['excess']*100:+.2f}%,命中率 {combo['hit_rate']*100:.1f}%",
            "",
            "**各策略单独**:",
        ]
        for sid in strategy_ids:
            s = per[sid]
            if compact:
                # 紧凑: 每策略一行短
                lines.append(f"- {sid}: 超额 {s['excess']*100:+.2f}% (触发 {s['signal_count']})")
            else:
                lines.append(
                    f"- {sid}: 触发 {s['signal_count']} 次,超额 {s['excess']*100:+.2f}%,"
                    f"命中率 {s['hit_rate']*100:.1f}%"
                )
        # 结论
        if combo["signal_count"] > 0:
            best_single = max(per.values(), key=lambda x: x["excess"])
            if combo["excess"] > best_single["excess"]:
                lines.append(f"\n💡 组合({mode.upper()})超额 {combo['excess']*100:+.2f}% "
                             f"优于最佳单策略 {best_single['excess']*100:+.2f}%,组合有效")
            else:
                lines.append(f"\n⚠️ 组合({mode.upper()})超额 {combo['excess']*100:+.2f}% "
                             f"未超过最佳单策略 {best_single['excess']*100:+.2f}%")
        else:
            # 0 信号: AND 模式常因条件过严
            if mode == "and":
                lines.append("\n⚠️ 组合(AND)无同日触发信号,条件过严。"
                             "建议改 OR 模式,或换相关度低的策略组合")
            else:
                lines.append("\n⚠️ 组合(OR)无任何触发信号,各策略可能均无信号")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 组合回测出错: {e}"


def handler_scan_with_strategy(
    strategy_id: str, top_n: int = 20, min_amount_yi: float = 0.5, limit: int = 0
) -> str:
    """全市场扫描指定策略,返回当日触发 buy 信号的股票列表(选股)。

    与 analyze_with_strategy(判断个股) 反向:这里是"给定策略找股票"。
    耗时约 5-30 分钟(全市场 4700 只),单线程跑(策略函数非线程安全)。
    """
    try:
        import strategy_engine as se
        log.info(
            "开始策略选股 %s, top_n=%d, min_amount_yi=%s, limit=%d (耗时约 5-30 分钟)",
            strategy_id, top_n, min_amount_yi, limit,
        )

        # 进度回调:每 200 只发一次进度消息(通过当前线程的 chat_id)
        chat_id = _current_chat_id()
        bot_ref = _current_bot()  # 拿到 FeishuBot 实例(若在飞书消息处理中)
        last_progress_ts = [0.0]

        def _progress_cb(scanned, total, hits_count):
            import time as _t
            now = _t.time()
            # 限频:至少间隔 30s 发一次进度,避免刷屏
            if now - last_progress_ts[0] < 30 and scanned != total:
                return
            last_progress_ts[0] = now
            pct = scanned * 100 // total if total else 0
            msg = f"⏳ 策略选股进度: {scanned}/{total} ({pct}%) | 命中 buy 信号 {hits_count} 只"
            if bot_ref and chat_id:
                try:
                    bot_ref._send_text(chat_id, msg)
                except Exception:
                    pass

        result = se.scan_with_strategy(
            strategy_id=strategy_id,
            top_n=top_n,
            min_amount_yi=min_amount_yi,
            limit=limit,
            progress_callback=_progress_cb if (bot_ref and chat_id) else None,
        )
        if "error" in result:
            return f"❌ {result['error']}"

        hits = result.get("hits", [])
        if not hits:
            return (
                f"🔍 **策略 {strategy_id} 全市场扫描完成**\n"
                f"- 扫描股票数: {result.get('scanned', 0)}\n"
                f"- 触发 buy 信号: 0 只\n"
                f"- 耗时: {result.get('elapsed_sec', 0):.0f}s\n"
                f"今日无股票触发 {strategy_id} 买入信号"
            )

        # 批量补股票名(用 stock_names 缓存,IN 查询)
        try:
            import stock_names as sn
            codes = [h["code"] for h in hits]
            name_map = sn.lookup_names(codes) if hasattr(sn, "lookup_names") else {}
            for h in hits:
                h["name"] = name_map.get(h["code"], "") or h.get("name", "")
        except Exception:
            pass

        lines = [
            f"🔍 **策略 {strategy_id} 全市场选股结果**",
            f"- 扫描股票数: {result.get('scanned', 0)}",
            f"- 触发 buy 信号: {result.get('hits_count', 0)} 只(显示前 {len(hits)})",
            f"- 耗时: {result.get('elapsed_sec', 0):.0f}s",
            "",
            "| 代码 | 名称 | 现价 | 涨幅 | 成交额(亿) | 触发理由 |",
            "|---|---|---|---|---|---|",
        ]
        for h in hits:
            reason = h.get("reason", "")
            # 截断长理由,避免表格过宽
            if len(reason) > 40:
                reason = reason[:38] + ".."
            lines.append(
                f"| {h['code']} | {h.get('name', '-')} | {h['price']} | "
                f"{h['pct']:+.2f}% | {h['amount_yi']} | {reason} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 策略选股出错: {e}"


def handler_get_stock_news(code: str, num: int = 15) -> str:
    """查询个股相关新闻(东财搜索接口,实时抓取)。

    输出精简: 默认只展开 top 8 条(title+time+source+summary),url 单独行省略
    避免超 600 字硬截断(15 条全展开 ~2500 字会被截到只剩 3-4 条)
    """
    try:
        import stock_names as sn
        from news_digest import fetch_stock_news

        # 1. 解析股票代码(支持股票名输入)
        resolved = sn.resolve_code(code) if code else None
        if not resolved:
            return f"❌ 无法识别股票: {code},请输入 6 位代码或股票名(如 301189 / 茅台)"
        # 2. 抓新闻
        news = fetch_stock_news(resolved, num=num, strict=True)
        if not news:
            return f"📰 未抓到 {resolved} 的相关新闻(可能暂无新闻或接口异常)"

        # 3. 反查股票名(若有)
        stock_name = ""
        try:
            import sqlite3
            conn = sqlite3.connect(str(sn.DB_PATH), timeout=5)
            row = conn.execute("SELECT name FROM stock_names WHERE code=?", (resolved,)).fetchone()
            conn.close()
            if row and row[0]:
                stock_name = row[0]
        except Exception:
            pass

        # 4. 输出精简: 只展开 top 8(超 600 字会被截断),summary 截到 80 字
        show_n = min(8, len(news))
        lines = [f"📰 **{resolved}{(' ' + stock_name) if stock_name else ''} 相关新闻**"
                 f"(共 {len(news)} 条,显示前 {show_n})"]
        for i, n in enumerate(news[:show_n], 1):
            summary = n["summary"] or ""
            if len(summary) > 80:
                summary = summary[:80] + "..."
            lines.append(
                f"\n{i}. **{n['title']}**\n"
                f"   {n['time']} · {n['source']} | {summary}"
            )
        if len(news) > show_n:
            lines.append(f"\n(其余 {len(news) - show_n} 条请去东财搜索 {resolved} 查看)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询新闻出错: {_friendly_err(e)}"


def handler_get_lhb(date: str = "", top_n: int = 20) -> str:
    """查询龙虎榜数据(东财龙虎榜接口)。"""
    try:
        from stock_market_extras import fetch_lhb, fmt_lhb

        rows = fetch_lhb(date_str=date if date else None, top_n=int(top_n))
        return fmt_lhb(rows)
    except Exception as e:
        return f"❌ 查询龙虎榜出错: {_friendly_err(e)}"


def handler_get_north_flow(days: int = 5) -> str:
    """查询北向资金(沪深股通)近 N 日净流入。"""
    try:
        from stock_market_extras import fetch_north_flow, fmt_north_flow

        rows = fetch_north_flow(days=int(days))
        return fmt_north_flow(rows)
    except Exception as e:
        return f"❌ 查询北向资金出错: {_friendly_err(e)}"


def handler_get_main_flow(code: str) -> str:
    """查询个股主力资金流(超大单/大单/中单/小单净流入)。"""
    try:
        import stock_names as sn
        from stock_market_extras import fetch_main_flow, fmt_main_flow

        resolved = sn.resolve_code(code) if code else None
        if not resolved:
            return f"❌ 无法识别股票: {code}"
        d = fetch_main_flow(resolved)
        return fmt_main_flow(d)
    except Exception as e:
        return f"❌ 查询主力资金流出错: {_friendly_err(e)}"


def handler_get_concept_sectors(code: str) -> str:
    """概念板块反查:给定股票反查它属于哪些板块。"""
    try:
        import stock_names as sn
        from stock_market_extras import fetch_concept_sectors, fmt_concept_sectors

        resolved = sn.resolve_code(code) if code else None
        if not resolved:
            return f"❌ 无法识别股票: {code}"
        sectors = fetch_concept_sectors(resolved)
        return fmt_concept_sectors(sectors)
    except Exception as e:
        return f"❌ 查询概念板块出错: {_friendly_err(e)}"


def handler_get_index(name: str = "") -> str:
    """查询指数行情(上证/深成/创业板/科创50/北证50)。"""
    try:
        from stock_market_extras import fetch_index, fmt_index

        data = fetch_index(name if name else None)
        return fmt_index(data)
    except Exception as e:
        return f"❌ 查询指数行情出错: {_friendly_err(e)}"


def handler_get_sector_flow(sector_type: str = "industry", top_n: int = 10) -> str:
    """查询行业/概念板块主力资金流排名。"""
    try:
        from stock_market_extras import fetch_sector_flow, fmt_sector_flow

        rows = fetch_sector_flow(sector_type, top_n)
        label = "行业板块" if sector_type == "industry" else "概念板块"
        return fmt_sector_flow(rows, label)
    except Exception as e:
        return f"❌ 查询板块资金流出错: {_friendly_err(e)}"


def handler_get_market_sentiment() -> str:
    """市场情绪速览:5大指数 + 行业板块资金流 Top5 + 概念板块 Top5。"""
    try:
        from stock_market_extras import fetch_market_sentiment, fmt_market_sentiment

        data = fetch_market_sentiment()
        return fmt_market_sentiment(data)
    except Exception as e:
        return f"❌ 查询市场情绪出错: {_friendly_err(e)}"


def handler_screen_stocks(
    pe_max: float | None = None, pe_min: float | None = None,
    pb_max: float | None = None, pb_min: float | None = None,
    mv_min_yi: float | None = None, mv_max_yi: float | None = None,
    top_n: int = 20, sort_by: str = "pe",
) -> str:
    """条件选股(PE/PB/市值筛选),东财 clist 接口拉取,耗时约 5-15 秒。"""
    try:
        from stock_market_extras import fmt_screen_result, screen_stocks

        rows = screen_stocks(pe_max, pe_min, pb_max, pb_min, mv_min_yi, mv_max_yi, top_n, sort_by)
        if isinstance(rows, dict) and "error" in rows:
            return f"⚠️ {rows['error']}"
        # 拼条件描述
        conds = []
        if pe_max is not None:
            conds.append(f"PE≤{pe_max}")
        if pe_min is not None:
            conds.append(f"PE≥{pe_min}")
        if pb_max is not None:
            conds.append(f"PB≤{pb_max}")
        if pb_min is not None:
            conds.append(f"PB≥{pb_min}")
        if mv_min_yi is not None:
            conds.append(f"市值≥{mv_min_yi}亿")
        if mv_max_yi is not None:
            conds.append(f"市值≤{mv_max_yi}亿")
        cond_str = " + ".join(conds) if conds else "无门槛"
        return fmt_screen_result(rows, cond_str)
    except Exception as e:
        return f"❌ 条件选股出错: {_friendly_err(e)}"


def handler_scan_with_yujie(top_n: int = 20, min_score: float = 5.0, limit: int = 0) -> str:
    """全市场玉姐评分实时扫描(用 daily 表已缓存数据,不联网,耗时 1-3 分钟)。

    与 get_yujie_picks(盘前 09:35 扫描结果)区别:这里实时重跑全市场评分。
    """
    try:
        import yujie_scan
        log.info(
            "开始玉姐全市场扫描, top_n=%d, min_score=%s, limit=%d (耗时约 1-3 分钟)",
            top_n, min_score, limit,
        )

        # 进度回调(同 scan_with_strategy)
        chat_id = _current_chat_id()
        bot_ref = _current_bot()
        last_progress_ts = [0.0]

        def _progress_cb(scanned, total, hits_count):
            import time as _t
            now = _t.time()
            if now - last_progress_ts[0] < 30 and scanned != total:
                return
            last_progress_ts[0] = now
            pct = scanned * 100 // total if total else 0
            msg = f"⏳ 玉姐全市场扫描: {scanned}/{total} ({pct}%) | 达标 {hits_count} 只"
            if bot_ref and chat_id:
                try:
                    bot_ref._send_text(chat_id, msg)
                except Exception:
                    pass

        result = yujie_scan.scan_all_cached(
            top_n=int(top_n),
            min_score=float(min_score),
            limit=int(limit),
            progress_callback=_progress_cb if (bot_ref and chat_id) else None,
        )

        hits = result.get("hits", [])
        # 门槛描述: min_score<=0 时是"无门槛 Top 排序",否则是"达标(≥X分)"
        threshold_desc = f"达标(≥{min_score:g}分)" if min_score > 0 else f"Top{len(hits)}(无门槛排序)"
        if not hits:
            return (
                f"🎯 **玉姐全市场扫描完成**\n"
                f"- 扫描: {result.get('scanned', 0)} 只\n"
                f"- {threshold_desc}: 0 只\n"
                f"- 耗时: {result.get('elapsed_sec', 0):.0f}s\n"
                f"当前无股票达到 {min_score:g} 分门槛,市场偏弱。可降低门槛(如 3 分)再试。"
            )

        # 批量补股票名(用 stock_names 缓存,IN 查询)
        try:
            import stock_names as sn
            codes = [h["code"] for h in hits]
            name_map = sn.lookup_names(codes) if hasattr(sn, "lookup_names") else {}
            for h in hits:
                h["name"] = name_map.get(h["code"], "")
        except Exception:
            for h in hits:
                h["name"] = ""

        lines = [
            f"🎯 **玉姐全市场实时扫描** 共扫 {result.get('scanned', 0)} 只,"
            f"{threshold_desc} {len(hits)} 只,耗时 {result.get('elapsed_sec', 0):.0f}s",
            "",
        ]
        for i, h in enumerate(hits, 1):
            hits_str = "、".join(h.get("hits", [])[:3])
            if len(h.get("hits", [])) > 3:
                hits_str += f"等{len(h['hits'])}条"
            name = h.get("name", "") or ""
            lines.append(
                f"{i}. **{h['code']} {name}** | {h['score']:g}分 | {hits_str}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ 玉姐扫描出错: {e}"


# 工具名 → 处理函数映射
TOOL_HANDLERS = {
    "analyze_stock": lambda args: handler_analyze(args.get("code", "")),
    "get_market_status": lambda args: handler_market(),
    "get_yujie_picks": lambda args: handler_yujie(
        args.get("min_score", 0), args.get("hit_rule", "")
    ),
    "get_portfolio": lambda args: handler_portfolio(),
    "get_finance": lambda args: handler_finance(args.get("code", "")),
    "compare_stocks": lambda args: handler_compare_stocks(args.get("codes", [])),
    "analyze_sector": lambda args: handler_analyze_sector(args.get("sector", "")),
    "query_history_picks": lambda args: handler_query_history_picks(args.get("date", "")),
    "manage_watchlist": lambda args: handler_watchlist(
        args.get("action", "list"),
        args.get("codes", []),
        session_id=_current_session_id(),
    ),
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
    "combo_backtest": lambda args: handler_combo_backtest(
        args.get("strategy_ids", []),
        args.get("mode", "and"),
        int(args.get("horizon", 20)),
        int(args.get("sample", 400)),
    ),
    "scan_with_strategy": lambda args: handler_scan_with_strategy(
        args.get("strategy_id", ""),
        int(args.get("top_n", 20)),
        float(args.get("min_amount_yi", 0.5)),
        int(args.get("limit", 0)),
    ),
    "get_stock_news": lambda args: handler_get_stock_news(
        args.get("code", ""), int(args.get("num", 15))
    ),
    # 市场数据 skill(新)
    "get_lhb": lambda args: handler_get_lhb(
        args.get("date", ""), int(args.get("top_n", 20))
    ),
    "get_north_flow": lambda args: handler_get_north_flow(int(args.get("days", 5))),
    "get_main_flow": lambda args: handler_get_main_flow(args.get("code", "")),
    "get_concept_sectors": lambda args: handler_get_concept_sectors(args.get("code", "")),
    "get_index": lambda args: handler_get_index(args.get("name", "")),
    "scan_with_yujie": lambda args: handler_scan_with_yujie(
        int(args.get("top_n", 20)),
        float(args.get("min_score", 5.0)),
        int(args.get("limit", 0)),
    ),
    "get_sector_flow": lambda args: handler_get_sector_flow(
        args.get("sector_type", "industry"),
        int(args.get("top_n", 10)),
    ),
    "get_market_sentiment": lambda args: handler_get_market_sentiment(),
    "screen_stocks": lambda args: handler_screen_stocks(
        args.get("pe_max"),
        args.get("pe_min"),
        args.get("pb_max"),
        args.get("pb_min"),
        args.get("mv_min_yi"),
        args.get("mv_max_yi"),
        int(args.get("top_n", 20)),
        args.get("sort_by", "pe"),
    ),
}

MAX_AGENT_STEPS = 6  # 最多 6 步推理(避免无限循环)

# 耗时工具(超过 10 秒),需先发"思考中"提示用户
SLOW_TOOLS = {"backtest_strategy", "grid_search_strategy", "scan_with_strategy", "scan_with_yujie", "combo_backtest"}

# 工具结果回灌给 LLM 时的字符上限(防止上下文污染,OpenClaw 风格)
TOOL_RESULT_MAX_CHARS = 6000

# 工具 schema 索引(name → parameters),用于参数预校验(Hermes 风格)
_TOOL_SCHEMA: dict[str, dict] = {
    t["function"]["name"]: t["function"].get("parameters", {})
    for t in TOOLS
}


# 网络类异常关键词(用于错误友好化)
_NET_ERR_KEYWORDS = ("502", "503", "504", "timeout", "timed out", "urlopen",
                     "HTTPError", "URLError", "ConnectionError", "Remote end")


def _friendly_err(e: Exception) -> str:
    """把技术异常转为用户友好提示。"""
    msg = str(e)
    if any(kw in msg for kw in _NET_ERR_KEYWORDS):
        return "接口暂时不可用,请稍后重试"
    if "代码" in msg and "6 位" in msg:
        return msg  # 参数错误,原文已友好
    if "未识别" in msg or "无法识别" in msg:
        return msg  # 股票名解析错误,原文已友好
    return f"内部错误,请稍后重试({type(e).__name__})"


def _validate_tool_args(fn_name: str, fn_args: dict) -> tuple[bool, str | None]:
    """对照 TOOLS schema 校验工具参数(Hermes 风格)。
    检查: required 字段是否齐全 + 基础类型匹配。
    返回 (ok, error_msg);ok=True 时 error_msg=None。
    """
    schema = _TOOL_SCHEMA.get(fn_name)
    if schema is None:
        return False, f"未知工具 '{fn_name}',请从已注册工具中选择"

    required = schema.get("required", [])
    missing = [r for r in required if r not in fn_args or fn_args[r] in (None, "")]
    if missing:
        return False, f"缺少必需参数: {missing}(schema 要求 {required})"

    # 基础类型校验:只校验已提供的字段,不强制 default
    type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}
    for k, v in fn_args.items():
        # 跳过 None(允许 null)
        if v is None:
            continue
        prop = schema.get("properties", {}).get(k)
        if prop is None:
            continue  # schema 未声明,放行(允许 LLM 加额外字段)
        expect = prop.get("type")
        py_type = type_map.get(expect)
        if py_type is None:
            continue  # 未知类型,放行
        # 特殊:bool 是 int 子类,integer/number 字段收到 bool 视为错
        if expect in ("integer", "number") and isinstance(v, bool):
            return False, f"参数 '{k}' 应为 {expect},实际 boolean({v})"
        if not isinstance(v, py_type):
            return False, f"参数 '{k}' 应为 {expect},实际 {type(v).__name__}({v!r})"
    return True, None


def _truncate_tool_result(text: str, max_chars: int = TOOL_RESULT_MAX_CHARS) -> str:
    """截断过长的工具结果,防止上下文污染(OpenClaw 风格)。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text
    # 保留前 max_chars 字符(通常前段是最重要的摘要/结论)
    return f"{text[:max_chars]}\n\n…[结果已截断,原始 {len(text)} 字]"


# 会话级并发锁(OpenClaw session lane):同一 session_id 的 Agent 推理串行,
# 防止用户连发消息时多个 Agent 并发跑、history 互相覆盖。
_session_locks: dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()
MAX_SESSION_LOCKS = 200  # 上限,防长期运行内存无界增长


def _prune_idle_session_locks() -> None:
    """移除空闲的会话锁(仅删当前未持有的),防 _session_locks 无界增长。

    必须在持有 _session_locks_guard 时调用。acquire(非阻塞)成功即证明该锁空闲,
    可安全删除;删除后若有线程再取该 session,会自动重建新锁。
    """
    for sid, lock in list(_session_locks.items()):
        if len(_session_locks) <= MAX_SESSION_LOCKS:
            break
        if lock.acquire(blocking=False):
            lock.release()
            del _session_locks[sid]


def _get_session_lock(session_id: str) -> threading.Lock:
    """获取(或创建)session 级别的锁。"""
    with _session_locks_guard:
        lock = _session_locks.get(session_id)
        if lock is None:
            if len(_session_locks) >= MAX_SESSION_LOCKS:
                _prune_idle_session_locks()
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock



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

    def _summarize_with_llm(self, text: str, max_tokens: int = 300) -> str:
        """调 LLM 做摘要,失败抛异常。无 tools,纯文本。"""
        import httpx
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": text}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30, trust_env=False) as c:
            r = c.post(self.base_url, json=payload, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"LLM 摘要 HTTP {r.status_code}")
        return (r.json()["choices"][0]["message"]["content"] or "").strip()

    def _compact_history(self, history: list) -> list:
        """压缩历史(OpenClaw compaction 风格):旧的几轮用 LLM 总结成一条摘要,
        保留最近 COMPACTION_KEEP_RECENT 条原文。

        - 阈值: COMPACTION_THRESHOLD 条(默认 10 = 5 轮)
        - 失败降级: 返回原 history,_save_history 会再走 _truncate_history 兜底
        - 单条消息已超 HISTORY_MSG_MAX_CHARS 的优先走 _truncate_history
        """
        if len(history) < COMPACTION_THRESHOLD:
            return history
        old = history[:-COMPACTION_KEEP_RECENT]
        recent = history[-COMPACTION_KEEP_RECENT:]
        try:
            # 拼接旧消息(每条截 300 字防 token 爆)
            text_parts = []
            for m in old:
                role = m.get("role", "user")
                c = (m.get("content") or "")[:300]
                text_parts.append(f"{role}: {c}")
            joined = "\n".join(text_parts)
            prompt = (
                "请用 200 字内总结以下对话的关键信息(涉及的股票代码、用户意图、"
                "已得到的分析结论),便于后续对话参考。只输出摘要正文,不要前缀:\n"
                + joined
            )
            summary = self._summarize_with_llm(prompt, max_tokens=300)
            if not summary:
                return history
            summary_msg = {"role": "assistant", "content": f"[历史摘要] {summary}"}
            log.info("历史压缩: %d 条 → 1 条摘要 + %d 条原文",
                     len(old), len(recent))
            return [summary_msg] + recent
        except Exception as e:
            log.warning("历史压缩失败 %s,降级到硬截断", e)
            return history

    def chat(self, user_text: str, history: list | None = None, session_id: str = "cli") -> tuple[str, list, list[bytes]]:
        """Agent 主循环: ReAct(Reason→Act→Observe)直到模型给出最终答案。

        Args:
            user_text: 用户输入
            history: 之前的对话历史(用于多轮)
            session_id: 会话 id(chat_id:sender),供 watchlist 等需用户隔离的 handler 用
        Returns:
            (reply_text, new_history, images)  images 是 PNG bytes 列表
        """
        import httpx

        # 每轮对话前清空图片队列
        _pending_images.clear()
        # 设置当前 session_id(供 watchlist handler 用,thread-local 隔离并发会话)
        _set_current_session_id(session_id)

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
                # 推理模型思考也占 token:1024 会"思考耗尽"输出空内容(8-27 事故),
                # 提高到 32768 保证思考完仍有内容;回复另有 600 字硬截断兜底
                "max_tokens": 32768,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            try:
                # 重试 2 次(共 3 次尝试):网络抖动/网关 5xx/间歇性 404,指数退避 1s/2s
                # 404 纳入重试: 网关(负载均衡)偶发 404 是瞬时的,重试即恢复(8-24 事故)
                r = None
                llm_start = time.time()
                last_code = None
                for attempt in range(3):
                    try:
                        with httpx.Client(timeout=60, trust_env=False) as c:
                            r = c.post(self.base_url, json=payload, headers=headers)
                        last_code = r.status_code
                        # 404(网关瞬时)/ 5xx(服务过载)重试;其余 2xx/4xx 不重试
                        if r.status_code != 404 and r.status_code < 500:
                            break
                        log.warning("LLM 调用 %d,重试 %d/3", r.status_code, attempt + 1)
                        r = None
                        time.sleep(2 ** attempt)  # 1s, 2s
                    except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                        log.warning("LLM 网络异常 %s,重试 %d/3", e, attempt + 1)
                        r = None
                        time.sleep(2 ** attempt)  # 1s, 2s
                if r is None:
                    _incr_stats("llm_calls", 1)
                    _incr_stats("llm_failures", 1)
                    _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
                    return (
                        f"⚠️ AI 暂时无响应(HTTP {last_code}),请稍后重试(已重试3次)",
                        new_history, list(_pending_images),
                    )
                if r.status_code != 200:
                    _incr_stats("llm_calls", 1)
                    _incr_stats("llm_failures", 1)
                    _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
                    return (
                        f"⚠️ AI 服务异常(HTTP {r.status_code}),请稍后重试",
                        new_history, list(_pending_images),
                    )
                msg = r.json()["choices"][0]["message"]
                _incr_stats("llm_calls", 1)
                _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
            except Exception as e:
                log.error("Agent LLM 调用失败: %s", e)
                _incr_stats("llm_calls", 1)
                _incr_stats("llm_failures", 1)
                _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
                return (
                    "⚠️ AI 调用异常,请稍后重试(已记录日志)",
                    new_history, list(_pending_images),
                )

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # 模型给出最终答案
                content = (msg.get("content") or "").strip()
                # 去除思考过程
                content = re.split(r"\n\s*(?:Thinking\s*Process|推理过程)[:：]", content)[0].strip()
                if not content:
                    # 空回复兜底: 推理模型思考耗尽/内容为空时,不能发空串给飞书
                    # (8-27 事故: 空串→飞书 230001→用户被晾 8 分钟)
                    content = "⚠️ 刚才没能生成有效回复,请换个问法或稍后重试。"
                if tool_log:
                    log.info("Agent 完成, 共 %d 步, 工具调用: %s", step + 1, tool_log)
                new_history.append({"role": "assistant", "content": content})
                # 历史压缩(OpenClaw compaction):旧轮 LLM 总结,降级到硬截断
                new_history = self._compact_history(new_history)
                return content, new_history, list(_pending_images)

            # 有工具调用: 执行并把结果回灌
            # 注意: assistant 消息需保留 tool_calls 字段,OpenAI 规范要求
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })

            # 清空上一轮的图片,只保留本轮工具调用生成的图。
            # 原因: ReAct 循环中 LLM 可能先猜错代码再纠正(如 301395→688395),
            # 若不清空,错误代码的 K 线图也会发给用户("发很多东西")。
            # 清空后只保留最后一轮(正确)的图片。
            _pending_images.clear()

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                t_start = time.time()
                try:
                    fn_args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError as e:
                    # 参数 JSON 解析失败 → 自愈:回灌错误让 LLM 修
                    log.warning("Agent step %d %s 参数 JSON 解析失败: %s", step + 1, fn_name, e)
                    tool_log.append(f"{fn_name}!(bad_json)")
                    _log_tool_call(session_id, step + 1, fn_name, {},
                                   0, int((time.time() - t_start) * 1000),
                                   error=f"bad_json: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"参数 JSON 解析失败: {e}。请用合法 JSON 重新调用 {fn_name}。",
                    })
                    continue

                log.info("Agent step %d 调用 %s(%s)", step + 1, fn_name, fn_args)
                tool_log.append(fn_name)

                # 参数预校验(Hermes 风格):不合法直接回灌,不浪费一次执行
                ok, err = _validate_tool_args(fn_name, fn_args)
                if not ok:
                    log.warning("Agent step %d %s 参数校验失败: %s", step + 1, fn_name, err)
                    _log_tool_call(session_id, step + 1, fn_name, fn_args,
                                   0, int((time.time() - t_start) * 1000),
                                   error=f"validate: {err}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"参数校验失败: {err}。请用正确参数重新调用 {fn_name}。",
                    })
                    continue

                handler = TOOL_HANDLERS.get(fn_name)
                if handler is None:
                    _log_tool_call(session_id, step + 1, fn_name, fn_args,
                                   0, int((time.time() - t_start) * 1000),
                                   error="unknown_tool")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"错误: 未知工具 '{fn_name}',请从已注册工具列表中选择。",
                    })
                    continue

                try:
                    result = handler(fn_args)
                    err_msg = None
                except Exception as e:
                    # 工具执行异常 → 自愈:回灌友好错误让 LLM 修参数或换工具
                    log.error("工具 %s 执行异常: %s", fn_name, e)
                    tool_log[-1] = f"{fn_name}!(exec_err)"
                    err_msg = f"exec: {e}"
                    result = (
                        f"工具 {fn_name} 执行失败: {e}。"
                        "请检查参数(如 code 是否为 6 位数字股票代码)后重试,"
                        "或换一个表述清晰的用户意图重新回答。"
                    )

                # 工具结果截断(OpenClaw 风格):防止上下文污染
                result = _truncate_tool_result(result)
                # 给 LLM 加压缩提示:不要复述全部数据,只取关键信息
                if isinstance(result, str) and len(result) > 200:
                    result = result + "\n\n[提示: 以上数据请精简总结给用户,只保留关键结论和数字,不要复述全部]"

                # 结构化日志(JSONL)
                _log_tool_call(session_id, step + 1, fn_name, fn_args,
                               len(result) if isinstance(result, str) else 0,
                               int((time.time() - t_start) * 1000),
                               error=err_msg)

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


def _split_long_text(text: str, max_len: int = 3800) -> list[str]:
    """按段落边界拆分长文本,尽量在 \n\n 处断开。单段超长则硬切。"""
    if len(text) <= max_len:
        return [text]
    chunks = []
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


# 消息去重:飞书长连接可能推送同一消息多次(ws 重连/服务端重复推送),
# 用 message_id 做 LRU 缓存,避免同一问题回复两次。
_seen_message_ids: dict[str, float] = {}
_seen_msg_lock = threading.Lock()
SEEN_MSG_MAX = 500  # 最多保留 500 条,约 1 天活跃量
SEEN_MSG_TTL = 3600 * 4  # 4 小时过期


def _is_duplicate_message(msg_id: str) -> bool:
    """检查消息是否已处理过,未处理则记录返回 False,已处理返回 True。"""
    import time as _t
    now = _t.time()
    with _seen_msg_lock:
        # 过期清理
        if len(_seen_message_ids) > SEEN_MSG_MAX:
            cutoff = now - SEEN_MSG_TTL
            for k in list(_seen_message_ids.keys()):
                if _seen_message_ids[k] < cutoff:
                    del _seen_message_ids[k]
        if msg_id in _seen_message_ids:
            return True
        _seen_message_ids[msg_id] = now
        # 超 LRU 上限,删最老的
        if len(_seen_message_ids) > SEEN_MSG_MAX:
            oldest = min(_seen_message_ids, key=_seen_message_ids.get)
            del _seen_message_ids[oldest]
        return False


class FeishuBotClient:
    """飞书长连接机器人客户端。"""

    def __init__(self):
        cfg = load_config().get("feishu", {})
        self.app_id = cfg.get("app_id", "")
        self.app_secret = cfg.get("app_secret", "")
        if not self.app_id or not self.app_secret:
            raise RuntimeError("feishu app_id/app_secret 未配置")
        self.client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        # 注册到全局,供 handler 内部主动发消息(如 scan_with_strategy 进度提示)
        _set_current_bot(self)
        # 消息处理线程池: Agent 处理可能耗时 1-30 分钟(扫描/回测),
        # 阻塞 ws 事件线程会导致 ping timeout 断连(历史 32 次)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg")
        log.info("飞书 Bot 客户端已初始化, app_id=%s...", self.app_id[:10])

    def _needs_thinking_hint(self, text: str) -> tuple[bool, str]:
        """轻量判断:用户问题是否触发了耗时工具,返回 (需提示, 提示文案)。

        用关键词匹配,避免额外 LLM 调用。
        分两档: 真正慢工具(回测/扫描,1-30分钟) vs 快查询(对比/新闻,1-10秒)
        """
        # 真正慢工具(>30s):回测/寻优/全市场扫描/组合回测
        very_slow = (
            "回测", "寻优", "调参", "网格", "最优参数", "backtest", "grid",
            "组合回测", "组合测试", "同时触发", "combo",
            "扫描整个市场", "全市场扫描", "全市场玉姐", "重新扫", "scan_with_yujie",
        )
        # 快查询(1-10s):多股对比/板块/新闻/资金流(无提示或短提示)
        # 这些不需要"思考 1-2 分钟"提示,实际很快
        text_lower = text.lower()
        if any(kw in text_lower for kw in very_slow):
            return True, "🤔 正在跑回测/扫描,需要 1-3 分钟,请耐心等待..."
        return False, ""

    def _reply_text(self, chat_id: str, text: str):
        """发送文本消息到 chat_id。超长自动分段(按段落边界拆分)。"""
        # 飞书文本消息长度上限约 4096,超过则按段落边界拆分多条发送
        if len(text) <= 4000:
            self._send_text(chat_id, text)
            return

        # 按段落(\n\n)拆分,尽量不破坏 markdown 结构
        chunks = _split_long_text(text, max_len=3800)
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                chunk = f"(第{i}/{len(chunks)}段)\n\n{chunk}" if i > 1 else chunk
            self._send_text(chat_id, chunk)
        log.info("长文本拆分: %d 字 → %d 段", len(text), len(chunks))

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
        """处理收到的消息事件(轻量:去重后立即丢线程池,不阻塞 ws 心跳)。

        Agent 处理(扫描/回测)可能耗时 1-30 分钟,若在 ws 事件线程里同步执行,
        会阻塞 ping/pong 导致连接被服务端掐断(ping timeout)。所以这里只做
        去重(必须同步,防重投递被并发处理),其余全部提交线程池。
        """
        try:
            msg = data.event.message
            chat_id = msg.chat_id
            msg_type = msg.message_type
            content_str = msg.content
            sender = data.event.sender.sender_id.open_id

            # 消息去重:飞书长连接可能推送同一消息多次(ws 重连/服务端重复推送),
            # 用 message_id 做幂等,避免同一问题回复两次。
            msg_id = msg.message_id or ""
            if msg_id and _is_duplicate_message(msg_id):
                log.info("跳过重复消息 msg_id=%s", msg_id)
                return

            # chat_type: 'p2p' 私聊 / 'group' 群聊,传给 worker 线程设 thread-local
            chat_type = getattr(msg, "chat_type", "") or "group"

            # 重活丢线程池:ws 事件线程立即返回,继续处理 ping/pong
            self._executor.submit(
                self._process_message, chat_id, msg_type, content_str, sender, chat_type
            )
        except Exception as e:
            log.error("提交消息处理失败: %s\n%s", e, traceback.format_exc())

    def _process_message(
        self, chat_id: str, msg_type: str, content_str: str, sender: str, chat_type: str
    ) -> None:
        """实际的消息处理(在线程池中执行)。"""
        try:
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

            # thread-local 必须在 worker 线程里设置(chat_type/chat_id/bot)
            _set_current_chat_type(chat_type)

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
            #
            # 会话级并发锁(OpenClaw session lane):同一 session_id 串行执行,
            # 防止用户连发消息时多个 Agent 并发跑、history 互相覆盖。
            # 优化: 锁等待 3 秒,拿不到立即提示"忙",避免连发消息被卡 5 分钟。
            session_lock = _get_session_lock(session_id)
            acquired = session_lock.acquire(timeout=3)
            if not acquired:
                self._reply_text(
                    chat_id,
                    "⏳ 上一条消息还在处理中,请稍等几秒再发。",
                )
                return
            try:
                agent = FeishuAgent()
                history = _load_history(session_id)
                # 先看 LLM 第一步是否要调慢工具:用轻量探测(同 LLM 但只取 tool_calls)
                need_thinking_hint, hint_text = self._needs_thinking_hint(text)
                if need_thinking_hint:
                    self._reply_text(chat_id, hint_text)
                # 设置 chat_id 到 thread-local,供 handler 内部主动发消息(如进度提示)
                _set_current_chat_id(chat_id)
                _set_current_bot(self)
                reply, new_history, images = agent.chat(text, history=history, session_id=session_id)
                _save_history(session_id, new_history)
            except Exception as e:
                log.warning("Agent 异常 %s, 降级到关键词路由", e)
                _, reply = route(text)
                images = []
            finally:
                session_lock.release()

            # 空回复防御:不发空串给飞书(230001 invalid message content)
            if not reply or not reply.strip():
                reply = "⚠️ 刚才没能生成有效回复,请换个问法或稍后重试。"
            log.info("回复长度 %d, 附图 %d 张", len(reply), len(images))
            # 最终回复硬截断:超 600 字截断,避免飞书消息过长
            if len(reply) > 600:
                reply = reply[:590] + "\n\n…(内容过长已截断,详情可继续问)"
                log.info("回复截断 %d → 600", len(reply))
            self._reply_text(chat_id, reply)
            # 发送图片
            for png in images:
                self._reply_image(chat_id, png)
        except Exception as e:
            log.error("处理消息异常: %s\n%s", e, traceback.format_exc())
            try:
                self._reply_text(chat_id, f"❌ 处理消息出错: {e}")
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
            auto_reconnect=True,  # ping timeout 后自动重连(默认 False,会导致 Bot 假死)
        )
        _register_stats_signal()
        _print_stats()  # 启动时打印一次
        log.info("启动飞书长连接(auto_reconnect=True),等待群里 @机器人 提问...")
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
