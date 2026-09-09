"""Bot 运行时共享上下文: thread-local 会话状态 + 运行统计。

feishu_bot(handlers 之后的 Agent/Client)与 bot_handlers(工具 handler)
共用的进程级状态,单独成模块避免互相 import。
monkeypatch 本模块即统一替换(patch 点收敛到一处)。
"""

import logging
import threading
from datetime import datetime

log = logging.getLogger("feishu_bot.ctx")







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


# ---- 会话级 thread-local 状态 ----

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
