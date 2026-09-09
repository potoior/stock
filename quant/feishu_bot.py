"""飞书应用 Bot 长连接机器人: 在群里 @机器人 提问,机器人回复。

工作模式: Function Calling Agent (LLM 自主决策调用工具,多步推理)
  用户问题 → LLM 决策 → 调用工具(analyze/market/yujie/portfolio) →
  LLM 整理结果 → 回复用户(可多轮调用)

工具(由 LLM 自动选择调用):
  analyze_stock(code)  个股技术面分析(45策略信号)
  get_market_status()  今日市场概况
  get_yujie_picks()    今日玉姐精选 Top10
  get_portfolio()      模拟持仓管理(买入/卖出/持仓/清仓)

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
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)

# 运行时上下文(thread-local 会话状态 + 统计)与工具 handlers 已拆分到独立模块
# 此处 re-export 保持 `feishu_bot.XXX` 调用方/测试兼容
from bot_context import (  # noqa: F401
    _STATS,
    _STATS_LOCK,
    MAX_TRACKED_SESSIONS,
    Ctx,
    _incr_stats,
    _print_stats,
    _register_stats_signal,
    _stats_add_session,
)
from bot_handlers import (  # noqa: F401
    PORTFOLIO_DB,
    REPORTS_DIR,
    TOOL_HANDLERS,
    WATCHLIST_DB,
    _fetch_sector_index,
    _fetch_sector_members,
    _lookup_library_strategy,
    _normalize_date,
    _portfolio_db,
    _resolve_stock_arg,
    _rollback_to_weekday,
    _watchlist_db,
    handler_ai,
    handler_analyze,
    handler_analyze_news_impact,
    handler_analyze_sector,
    handler_analyze_with_strategies,
    handler_analyze_with_strategy,
    handler_analyze_with_yujie,
    handler_backtest_strategy,
    handler_combo_backtest,
    handler_compare_stocks,
    handler_compile_strategy,
    handler_enable_library_strategy,
    handler_finance,
    handler_get_concept_sectors,
    handler_get_index,
    handler_get_lhb,
    handler_get_main_flow,
    handler_get_market_sentiment,
    handler_get_north_flow,
    handler_get_sector_flow,
    handler_get_stock_news,
    handler_get_strategy_library,
    handler_get_yujie_detail,
    handler_grid_search,
    handler_list_strategies,
    handler_market,
    handler_portfolio,
    handler_query_history_picks,
    handler_scan_combo,
    handler_scan_custom,
    handler_scan_with_strategy,
    handler_scan_with_yujie,
    handler_screen_stocks,
    handler_set_strategy_params,
    handler_toggle_strategy,
    handler_watchlist,
    handler_yujie,
    load_config,
    portfolio_buy,
    portfolio_clear,
    portfolio_list,
    portfolio_sell,
    watchlist_add,
    watchlist_group_add,
    watchlist_group_list,
    watchlist_group_remove,
    watchlist_list,
    watchlist_remove,
)
from feishu_bot_tools import SYSTEM_PROMPT, TOOLS  # noqa: E402,F401  (re-export 保持兼容)

MAX_AGENT_STEPS = 6  # 最多 6 步推理(避免无限循环)

# 耗时工具(超过 10 秒),需先发"思考中"提示用户
SLOW_TOOLS = {"backtest_strategy", "grid_search_strategy", "scan_with_strategy", "scan_with_yujie", "combo_backtest", "scan_combo", "screen_stocks"}

# 工具结果回灌给 LLM 时的字符上限(防止上下文污染,OpenClaw 风格)
TOOL_RESULT_MAX_CHARS = 10000

# 工具 schema 索引(name → parameters),用于参数预校验(Hermes 风格)
_TOOL_SCHEMA: dict[str, dict] = {
    t["function"]["name"]: t["function"].get("parameters", {})
    for t in TOOLS
}

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
AGENT_DB = ENGINE_HOME / "agent_data.db"

# 结构化工具调用日志(JSONL),便于审计/统计(OpenClaw 风格)
TOOL_AUDIT_LOG = ENGINE_HOME / "logs" / "feishu_bot_audit.jsonl"
TOOL_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 单文件 10MB,超出轮转
TOOL_AUDIT_KEEP = 3  # 保留备份数


def _rotate_audit_log_if_needed() -> None:
    """审计日志超过大小上限时轮转(xxx.1 最新 → xxx.3 最旧)。失败不影响主流程。"""
    try:
        if not TOOL_AUDIT_LOG.exists() or TOOL_AUDIT_LOG.stat().st_size < TOOL_AUDIT_MAX_BYTES:
            return
        for i in range(TOOL_AUDIT_KEEP - 1, 0, -1):
            src = Path(f"{TOOL_AUDIT_LOG}.{i}")
            dst = Path(f"{TOOL_AUDIT_LOG}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        TOOL_AUDIT_LOG.rename(Path(f"{TOOL_AUDIT_LOG}.1"))
    except Exception:
        pass


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
        TOOL_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        _rotate_audit_log_if_needed()
        with open(TOOL_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计日志失败不影响主流程
    # 同步累加运行状态
    _incr_stats("tool_calls", 1)
    if error:
        _incr_stats("tool_failures", 1)
    _stats_add_session(session_id)


# 命令路由关键字
CMD_ANALYZE = ("分析", "看看", "看看股")
CMD_MARKET = ("市场", "大盘", "行情", "今日")
CMD_YUJIE = ("玉姐", "候选", "精选", "top")
CMD_PORTFOLIO = ("持仓", "portfolio", "仓位", "股票池")


# 跨轮对话历史持久化(按 session_id 存储,sqlite)
# 保留最近 MAX_HISTORY_TURNS 轮(1 轮 = user + assistant 两条消息)
# 超过 HISTORY_EXPIRE_DAYS 天未活跃的 session 自动清理(启动时跑一次)
HISTORY_DB = ENGINE_HOME / "agent_history.db"
MAX_HISTORY_TURNS = 12
HISTORY_EXPIRE_DAYS = 7


def _history_db():
    """对话历史 sqlite 连接(启用 WAL,防高频对话锁竞争)。"""
    conn = sqlite3.connect(str(HISTORY_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


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
HISTORY_MSG_MAX_CHARS = 2000
# 裁剪时保留的字符合额:头部 + 尾部(结论/表格末行常在尾部,纯砍头会丢关键信息)
HISTORY_MSG_KEEP_HEAD = 1300
HISTORY_MSG_KEEP_TAIL = 500

# 飞书最终回复兜底截断(正常长度走 _reply_text 分段,此为防 LLM 失控的保险丝)
REPLY_HARD_LIMIT_CHARS = 12000

# 历史压缩(Compaction, OpenClaw 风格):历史达到 N 条时,把最旧的几轮用 LLM 总结成 1 条
# 触发阈值: 24 条(12 轮);压缩后: 1 条摘要 + 最近 18 条(9 轮)原文
# 注: 大上下文模型(1M)下放宽,压缩频率不高(压缩后 3 轮才再触发,省 LLM 调用)
COMPACTION_THRESHOLD = 24
COMPACTION_KEEP_RECENT = 18


def _truncate_history(history: list) -> list:
    """裁剪历史中的超长 assistant 消息,保留头尾 + 截断标记。

    user 消息通常很短不裁剪,只裁 assistant(可能含完整回测报告/策略列表)。
    结论和表格末行常在消息尾部,纯砍头会丢关键信息,故保留头部+尾部。
    """
    out = []
    for m in history:
        c = m.get("content", "")
        if m.get("role") == "assistant" and len(c) > HISTORY_MSG_MAX_CHARS:
            c = (
                c[:HISTORY_MSG_KEEP_HEAD]
                + "\n…(中间省略)…\n"
                + c[-HISTORY_MSG_KEEP_TAIL:]
                + "…(已截断)"
            )
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


# ============ 命令分发 ============


def route(text: str, ctx=None) -> tuple[str, str]:
    """关键词降级路由(仅在 Agent 异常时兜底)。

    Returns:
        (handler_name, formatted_reply)
    """
    ctx = ctx or Ctx()
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
        return "analyze", handler_analyze(ctx, code)

    # 2. 市场概况
    if any(k in text for k in CMD_MARKET):
        return "market", handler_market(ctx)

    # 3. 玉姐候选
    if any(k in text for k in CMD_YUJIE):
        return "yujie", handler_yujie(ctx)

    # 4. 持仓查询
    if any(k in text for k in CMD_PORTFOLIO):
        return "portfolio", handler_portfolio(ctx, "list")

    # 5. AI 自由问答
    return "ai", handler_ai(ctx, text)


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

        from ai_decider import build_endpoints, load_env
        load_env()
        self.endpoints = build_endpoints()
        self.api_key = os.environ.get("AI_API_KEY", "")
        self.base_url = self.endpoints[0]["url"]
        self.model = self.endpoints[0]["model"]
        if not self.api_key or not self.base_url:
            raise RuntimeError("AI_API_KEY/AI_BASE_URL 未配置(检查 .env)")

    def _post_llm(self, payload: dict, timeout: int = 60):
        """依次尝试主/备网关 POST。返回 (response|None, 最后状态码)。

        200 返回;404/429/5xx/网络异常切换下一端点;其余 4xx(配置类)不切换。
        """
        import httpx
        resp = None
        last_code = None
        for ep in self.endpoints:
            headers = {
                "Authorization": f"Bearer {ep['key']}",
                "Content-Type": "application/json",
            }
            try:
                with httpx.Client(timeout=timeout, trust_env=False) as c:
                    resp = c.post(ep["url"], json={**payload, "model": ep["model"]}, headers=headers)
                last_code = resp.status_code
                if resp.status_code == 200 or (
                    resp.status_code < 500 and resp.status_code not in (404, 429)
                ):
                    return resp, last_code
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                continue  # 网络异常 → 试下一端点
            resp = None  # 404/429/5xx → 试下一端点
        return resp, last_code

    def _summarize_with_llm(self, text: str, max_tokens: int = 300) -> str:
        """调 LLM 做摘要,失败抛异常。无 tools,纯文本。"""
        payload = {
            "messages": [{"role": "user", "content": text}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        r, code = self._post_llm(payload, timeout=30)
        if r is None or r.status_code != 200:
            raise RuntimeError(f"LLM 摘要 HTTP {code}")
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

    def chat(self, user_text: str, ctx=None, history: list | None = None, session_id: str = "cli") -> tuple[str, list, list[bytes]]:
        """Agent 主循环: ReAct(Reason→Act→Observe)直到模型给出最终答案。

        Args:
            user_text: 用户输入
            ctx: 会话上下文(Ctx,显式传参);None 时用默认 CLI 上下文
            history: 之前的对话历史(用于多轮)
            session_id: 会话 id(chat_id:sender),供 watchlist 等需用户隔离的 handler 用
        Returns:
            (reply_text, new_history, images)  images 是 PNG bytes 列表
        """
        ctx = ctx or Ctx(session_id=session_id)

        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        new_history = (history or []) + [{"role": "user", "content": user_text}]
        tool_log = []

        for step in range(MAX_AGENT_STEPS):
            payload = {
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.3,
                # 推理模型思考也占 token:1024 会"思考耗尽"输出空内容(8-27 事故),
                # 提高到 32768 保证思考完仍有内容;回复超长由 REPLY_HARD_LIMIT_CHARS 兜底
                "max_tokens": 32768,
            }

            try:
                # 重试 2 次(共 3 次尝试):网络抖动/网关 5xx/间歇性 404,指数退避 1s/2s
                # 每次尝试内部会先主后备切换(_post_llm),全部失败才进入下一次重试
                r = None
                llm_start = time.time()
                last_code = None
                for attempt in range(3):
                    r, last_code = self._post_llm(payload, timeout=60)
                    if r is not None:
                        break
                    log.warning("LLM 调用 %s,重试 %d/3", last_code, attempt + 1)
                    time.sleep(2 ** attempt)  # 1s, 2s
                if r is None:
                    _incr_stats("llm_calls", 1)
                    _incr_stats("llm_failures", 1)
                    _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
                    return (
                        f"⚠️ AI 暂时无响应(HTTP {last_code}),请稍后重试(已重试3次)",
                        new_history, list(ctx.images),
                    )
                if r.status_code != 200:
                    _incr_stats("llm_calls", 1)
                    _incr_stats("llm_failures", 1)
                    _incr_stats("llm_total_ms", int((time.time() - llm_start) * 1000))
                    return (
                        f"⚠️ AI 服务异常(HTTP {r.status_code}),请稍后重试",
                        new_history, list(ctx.images),
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
                    new_history, list(ctx.images),
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
                return content, new_history, list(ctx.images)

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
            ctx.images.clear()

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
                    result = handler(ctx, fn_args)
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
        return f"(推理步数已达上限,工具调用: {' → '.join(tool_log)}。请重新提问或换种问法。)", new_history, list(ctx.images)


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


def _extract_post_text(content: dict) -> str:
    """从富文本(post)消息中提取纯文本。

    post 结构: {"post": {"zh_cn": {"title": ..., "content": [[{tag,...}, ...], ...]}}}
    支持 zh_cn/zh_tw/en_us 等任意语言 key,取第一个。@提及丢弃(通常是 @机器人)。
    """
    post = (content or {}).get("post") or {}
    data = next(iter(post.values()), {})
    lines = []
    for para in data.get("content", []):
        parts = []
        for item in para:
            if item.get("tag") == "text":
                parts.append(item.get("text", ""))
            elif item.get("tag") == "a":
                parts.append(item.get("text", "") or item.get("href", ""))
        lines.append("".join(parts))
    return "\n".join(lines).strip()


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
        # 消息处理线程池: Agent 处理可能耗时 1-30 分钟(扫描/回测),
        # 阻塞 ws 事件线程会导致 ping timeout 断连(历史 32 次)
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="msg")
        log.info("飞书 Bot 客户端已初始化, app_id=%s...", self.app_id[:10])

    def _get_bot_open_id(self) -> str | None:
        """获取机器人自己的 open_id(缓存)。失败返回 None,下次重试。"""
        if getattr(self, "_bot_open_id", None):
            return self._bot_open_id
        try:
            from feishu import FeishuBot

            self._bot_open_id = FeishuBot().get_bot_open_id()
            log.info("机器人 open_id=%s", self._bot_open_id)
        except Exception as e:
            log.warning("获取 bot open_id 失败: %s", e)
            self._bot_open_id = None
        return self._bot_open_id

    def _mentions_bot(self, mentions: list | None) -> bool:
        """消息的 @提及 里是否包含本机器人。"""
        if not mentions:
            return False
        bot_id = self._get_bot_open_id()
        for m in mentions:
            mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
            if mid and mid == bot_id:
                return True
        # 拿不到 open_id 时降级:有任何提及就算(宁可多答不误伤 @机器人)
        return bot_id is None and len(mentions) > 0

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

            # @提及信息(判断群聊是否 @ 了本机器人)
            mentions = getattr(msg, "mentions", None) or []

            # 重活丢线程池:ws 事件线程立即返回,继续处理 ping/pong
            self._executor.submit(
                self._process_message, chat_id, msg_type, content_str, sender, chat_type, mentions
            )
        except Exception as e:
            log.error("提交消息处理失败: %s\n%s", e, traceback.format_exc())

    def _process_message(
        self, chat_id: str, msg_type: str, content_str: str, sender: str, chat_type: str,
        mentions: list | None = None,
    ) -> None:
        """实际的消息处理(在线程池中执行)。"""
        try:
            # 群聊只响应 @了本机器人 的消息,其余静默忽略
            if chat_type == "group" and not self._mentions_bot(mentions):
                log.info("群聊消息未@机器人,忽略: %s", content_str[:50] if content_str else "")
                return

            # 解析消息内容(text=普通文本 / post=富文本,其余类型不支持)
            try:
                content = json.loads(content_str)
            except Exception:
                content = None
            if msg_type == "text":
                text = (content.get("text") or "").strip() if content else ""
            elif msg_type == "post":
                text = _extract_post_text(content or {})
            else:
                self._reply_text(chat_id, "目前仅支持文本提问,例如:\n- 分析 600519\n- 市场\n- 玉姐\n- 持仓")
                return

            # 去掉 @机器人 的 mention tag (飞书文本里以 @_<user_id> 形式存在)
            text = re.sub(r"@_\w+\s*", "", text).strip()
            if not text:
                self._reply_text(chat_id, "请输入您的问题,例如:\n- 分析 600519\n- 市场\n- 玉姐\n- 持仓")
                return

            log.info("收到消息 chat=%s sender=%s text=%r", chat_id, sender, text[:100])

            # 会话上下文(显式传参,worker 线程内构造)
            ctx = Ctx(session_id=f"{chat_id}:{sender}", chat_id=chat_id,
                      chat_type=chat_type, bot=self)

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
                reply, new_history, images = agent.chat(text, ctx=ctx, history=history, session_id=session_id)
                _save_history(session_id, new_history)
            except Exception as e:
                log.warning("Agent 异常 %s, 降级到关键词路由", e)
                _, reply = route(text, ctx)
                images = []
            finally:
                session_lock.release()

            # 空回复防御:不发空串给飞书(230001 invalid message content)
            if not reply or not reply.strip():
                reply = "⚠️ 刚才没能生成有效回复,请换个问法或稍后重试。"
            log.info("回复长度 %d, 附图 %d 张", len(reply), len(images))
            # 超长内容交给 _reply_text 按段落自动分段发送(4000 字/条);
            # 此处仅防 LLM 失控输出超长内容的兜底截断
            if len(reply) > REPLY_HARD_LIMIT_CHARS:
                reply = reply[:REPLY_HARD_LIMIT_CHARS] + "\n\n…(内容过长已截断,详情可继续问)"
                log.info("回复截断 %d → %d", len(reply), REPLY_HARD_LIMIT_CHARS)
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
