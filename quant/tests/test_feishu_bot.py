"""feishu_bot 新功能单测: 历史裁剪/分段、自选股、股票名识别。

不联网,所有外部依赖 mock 或用临时 db。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import feishu_bot
import stock_names
from feishu_bot import (
    _get_session_lock,
    _incr_stats,
    _is_reset_command,
    _log_tool_call,
    _normalize_date,
    _print_stats,
    _rollback_to_weekday,
    _split_long_text,
    _stats_add_session,
    _truncate_history,
    _truncate_tool_result,
    _validate_tool_args,
    handler_analyze_sector,
    handler_compare_stocks,
    handler_query_history_picks,
    handler_watchlist,
    watchlist_add,
    watchlist_list,
    watchlist_remove,
)

# ============ 历史裁剪 ============


def test_truncate_history_long_assistant():
    """超过 500 字的 assistant 消息应被裁剪到 200 字 + 截断标记。"""
    long_content = "A" * 800
    h = [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": long_content},
    ]
    out = _truncate_history(h)
    assert len(out[1]["content"]) < 250  # 200 字 + 截断标记
    assert "已截断" in out[1]["content"]
    assert out[0]["content"] == "问"  # user 不裁剪


def test_truncate_history_short_kept():
    """短消息不裁剪。"""
    h = [
        {"role": "user", "content": "分析茅台"},
        {"role": "assistant", "content": "茅台卖出信号"},
    ]
    out = _truncate_history(h)
    assert out == h


def test_save_history_truncates(tmp_path, monkeypatch):
    """_save_history 落盘时应自动裁剪。"""
    db = tmp_path / "test_history.db"
    monkeypatch.setattr(feishu_bot, "HISTORY_DB", db)
    long_content = "X" * 1000
    h = [
        {"role": "user", "content": "Q"},
        {"role": "assistant", "content": long_content},
    ]
    feishu_bot._save_history("test_session", h)

    # 读回验证
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT history_json FROM agent_history WHERE session_id=?", ("test_session",)).fetchone()
    conn.close()
    loaded = json.loads(row[0])
    assert len(loaded[1]["content"]) < 250
    assert "已截断" in loaded[1]["content"]


def test_save_history_max_turns(tmp_path, monkeypatch):
    """历史超过 MAX_HISTORY_TURNS*2 条应自动截断到最近 N 条。"""
    db = tmp_path / "test_history.db"
    monkeypatch.setattr(feishu_bot, "HISTORY_DB", db)
    # 构造 20 轮 = 40 条
    h = []
    for i in range(20):
        h.append({"role": "user", "content": f"Q{i}"})
        h.append({"role": "assistant", "content": f"A{i}"})
    feishu_bot._save_history("test_session", h)

    loaded = feishu_bot._load_history("test_session")
    max_msgs = feishu_bot.MAX_HISTORY_TURNS * 2
    assert len(loaded) == max_msgs
    # 应保留最后 12 条
    assert loaded[0]["content"] == f"Q{20 - feishu_bot.MAX_HISTORY_TURNS}"


# ============ 历史自动过期 ============


def test_purge_old_history(tmp_path, monkeypatch):
    """超过 7 天的 session 应被清理,新 session 保留。"""
    db = tmp_path / "test_history.db"
    monkeypatch.setattr(feishu_bot, "HISTORY_DB", db)
    # 手动插入老 session
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE agent_history (
        session_id TEXT PRIMARY KEY, history_json TEXT, ts INTEGER)""")
    old_ts = int(time.time()) - 10 * 86400  # 10天前
    new_ts = int(time.time())
    conn.execute("INSERT INTO agent_history VALUES(?,?,?)", ("old", "[]", old_ts))
    conn.execute("INSERT INTO agent_history VALUES(?,?,?)", ("new", "[]", new_ts))
    conn.commit()
    conn.close()

    n = feishu_bot._purge_old_history()
    assert n == 1
    # 新 session 应保留
    loaded = feishu_bot._load_history("new")
    assert loaded == []


def test_purge_old_history_empty_db(tmp_path, monkeypatch):
    """空 db 启动时不报错。"""
    db = tmp_path / "test_history.db"
    monkeypatch.setattr(feishu_bot, "HISTORY_DB", db)
    n = feishu_bot._purge_old_history()
    assert n == 0


# ============ 长文本分段 ============


def test_split_long_text_short():
    """短文本不拆分。"""
    out = _split_long_text("短文本", max_len=3800)
    assert out == ["短文本"]


def test_split_long_text_long():
    """长文本按段落边界拆分,每段不超过 max_len。"""
    text = ""
    for i in range(20):
        text += f"## 第{i+1}段\n" + "A" * 250 + "\n\n"
    chunks = _split_long_text(text, max_len=3800)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 3800


def test_split_long_text_no_paragraph():
    """无段落分隔的超长文本,硬切。"""
    text = "X" * 5000
    chunks = _split_long_text(text, max_len=2000)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c) <= 2000


# ============ 重置命令识别 ============


def test_is_reset_command_true():
    """重置触发词应识别。"""
    for cmd in ["重置", "新话题", "忘了吧", "清空", "重新开始",
                "重置历史", "/reset", "/new", "/clear", "重置一下", "请清空"]:
        assert _is_reset_command(cmd), f"应识别 {cmd!r}"


def test_is_reset_command_false():
    """易混淆的非重置词不应误判。"""
    for cmd in ["分析茅台", "玉姐前10", "11-20呢", "重置BOLL参数",
                "清仓", "清空一下持仓", "忘了吧MACD", "重新分析茅台"]:
        assert not _is_reset_command(cmd), f"不应识别 {cmd!r}"


def test_is_reset_command_with_punct():
    """带标点的重置命令应识别。"""
    for cmd in ["重置。", "清空!", "忘了吧?", "重置,"]:
        assert _is_reset_command(cmd), f"应识别 {cmd!r}"


# ============ 自选股 ============


def _setup_watchlist_db(tmp_path, monkeypatch):
    db = tmp_path / "test_watchlist.db"
    monkeypatch.setattr(feishu_bot, "WATCHLIST_DB", db)
    return db


def test_watchlist_add_list_remove(tmp_path, monkeypatch):
    """自选股 增/查/删 全流程。"""
    _setup_watchlist_db(tmp_path, monkeypatch)

    # 加 2 只
    watchlist_add("userA", "600519", "茅台")
    watchlist_add("userA", "000858", "五粮液")
    items = watchlist_list("userA")
    assert len(items) == 2
    codes = {it["code"] for it in items}
    assert codes == {"600519", "000858"}

    # 删 1 只
    watchlist_remove("userA", "600519")
    items = watchlist_list("userA")
    assert len(items) == 1
    assert items[0]["code"] == "000858"


def test_watchlist_isolation(tmp_path, monkeypatch):
    """不同 session_id 应完全隔离。"""
    _setup_watchlist_db(tmp_path, monkeypatch)
    watchlist_add("userA", "600519", "茅台")
    watchlist_add("userB", "000858", "五粮液")
    assert len(watchlist_list("userA")) == 1
    assert len(watchlist_list("userB")) == 1
    assert watchlist_list("userA")[0]["code"] == "600519"
    assert watchlist_list("userB")[0]["code"] == "000858"


def test_watchlist_remove_nonexistent(tmp_path, monkeypatch):
    """删除不存在的自选应提示 0 条。"""
    _setup_watchlist_db(tmp_path, monkeypatch)
    msg = watchlist_remove("userA", "600519")
    assert "0 条" in msg or "没有" in msg


def test_watchlist_duplicate(tmp_path, monkeypatch):
    """重复添加同只应不报错(INSERT OR REPLACE)。"""
    _setup_watchlist_db(tmp_path, monkeypatch)
    watchlist_add("userA", "600519", "茅台")
    watchlist_add("userA", "600519", "茅台新名")  # 更新名称
    items = watchlist_list("userA")
    assert len(items) == 1
    assert items[0]["name"] == "茅台新名"


def test_handler_watchlist_list_empty(tmp_path, monkeypatch):
    """空自选应给提示。"""
    _setup_watchlist_db(tmp_path, monkeypatch)
    out = handler_watchlist("list", session_id="newUser")
    assert "为空" in out or "empty" in out


def test_handler_watchlist_no_codes(tmp_path, monkeypatch):
    """add/remove 不传 codes 应报错。"""
    _setup_watchlist_db(tmp_path, monkeypatch)
    out = handler_watchlist("add", session_id="userA")
    assert "需要" in out or "错误" in out or "codes" in out


# ============ 股票名称识别(缓存,mock 搜索) ============


def test_resolve_code_6digit():
    """6 位代码直接返回。"""
    assert stock_names.resolve_code("600519") == "600519"
    assert stock_names.resolve_code("000858") == "000858"


def test_resolve_code_cached(tmp_path, monkeypatch):
    """缓存命中时不查网络。"""
    db = tmp_path / "test_names.db"
    monkeypatch.setattr(stock_names, "DB_PATH", db)
    # 预填缓存
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE stock_names (
        query TEXT, code TEXT, name TEXT, ts INTEGER, PRIMARY KEY (query, name))""")
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)",
                 ("茅台", "600519", "贵州茅台", int(time.time())))
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)",
                 ("茅台", "600519", "茅台", int(time.time())))
    conn.commit()
    conn.close()

    # mock 搜索函数,如果被调用说明缓存没生效
    with patch("stock_names._search_tencent", side_effect=AssertionError("缓存未命中却调了搜索")):
        code = stock_names.resolve_code("茅台")
    assert code == "600519"


def test_resolve_code_search_fallback(tmp_path, monkeypatch):
    """缓存未命中时调搜索接口。"""
    db = tmp_path / "test_names.db"
    monkeypatch.setattr(stock_names, "DB_PATH", db)

    def mock_search(q):
        return [{"code": "600519", "name": "贵州茅台", "pinyin": "gzmt", "market": "sh"}]

    with patch("stock_names._search_tencent", side_effect=mock_search):
        code = stock_names.resolve_code("茅台")
    assert code == "600519"


def test_resolve_code_empty():
    """空输入应返回 None。"""
    assert stock_names.resolve_code("") is None
    assert stock_names.resolve_code(None) is None


def test_short_name():
    """地名前缀应被去掉。"""
    assert stock_names._short_name("贵州茅台") == "茅台"
    assert stock_names._short_name("中国平安") == "平安"
    assert stock_names._short_name("宁德时代") == "宁德时代"  # 不是地名前缀
    assert stock_names._short_name("") == ""


def test_resolve_codes_with_code_in_text():
    """文本中含 6 位代码应直接识别(不需要缓存)。"""
    # 用 mock 防止搜索被调用
    with patch("stock_names._search_tencent", return_value=[]):
        codes = stock_names.resolve_codes("看下 600519 怎么样")
    assert "600519" in codes


def test_resolve_codes_from_cache(tmp_path, monkeypatch):
    """文本中含缓存过的股票名应识别。"""
    db = tmp_path / "test_names.db"
    monkeypatch.setattr(stock_names, "DB_PATH", db)
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE stock_names (
        query TEXT, code TEXT, name TEXT, ts INTEGER, PRIMARY KEY (query, name))""")
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)",
                 ("茅台", "600519", "茅台", int(time.time())))
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)",
                 ("五粮液", "000858", "五粮液", int(time.time())))
    conn.commit()
    conn.close()

    with patch("stock_names._search_tencent", side_effect=AssertionError("不应调搜索")):
        codes = stock_names.resolve_codes("对比茅台和五粮液")
    assert "600519" in codes
    assert "000858" in codes


# ============ 参数 schema 预校验(Hermes 风格) ============


def test_validate_args_ok():
    """正常参数通过校验。"""
    ok, err = _validate_tool_args("analyze_stock", {"code": "600519"})
    assert ok is True
    assert err is None


def test_validate_args_missing_required():
    """缺必需参数应被拒。"""
    ok, err = _validate_tool_args("analyze_stock", {})
    assert ok is False
    assert "code" in err


def test_validate_args_empty_string():
    """空字符串等同缺失。"""
    ok, err = _validate_tool_args("analyze_stock", {"code": ""})
    assert ok is False
    assert "缺少" in err


def test_validate_args_wrong_type():
    """类型不符应被拒(code 应为 string,传 int)。"""
    ok, err = _validate_tool_args("analyze_stock", {"code": 600519})
    assert ok is False
    assert "应为" in err


def test_validate_args_bool_for_integer():
    """bool 传给 integer 字段应被拒(bool 是 int 子类,需特殊排除)。"""
    ok, err = _validate_tool_args("backtest_strategy", {"strategy_id": "macd", "sample": True})
    assert ok is False
    assert "integer" in err


def test_validate_args_unknown_tool():
    """未注册工具应被拒。"""
    ok, err = _validate_tool_args("nonexistent_tool", {})
    assert ok is False
    assert "未知工具" in err


def test_validate_args_no_required():
    """无 required 字段的工具,空 args 通过。"""
    ok, err = _validate_tool_args("get_market_status", {})
    assert ok is True


def test_validate_args_optional_only():
    """只有可选字段,不传也通过。"""
    ok, err = _validate_tool_args("get_yujie_picks", {})
    assert ok is True


def test_validate_args_optional_with_value():
    """可选字段给了值,类型对则通过。"""
    ok, err = _validate_tool_args("get_yujie_picks", {"min_score": 7})
    assert ok is True
    ok, err = _validate_tool_args("get_yujie_picks", {"min_score": 7.5})
    assert ok is True


def test_validate_args_optional_wrong_type():
    """可选字段类型错应被拒。"""
    ok, err = _validate_tool_args("get_yujie_picks", {"min_score": "7"})
    assert ok is False
    assert "应为" in err


# ============ 工具结果截断(OpenClaw 风格) ============


def test_truncate_result_short():
    """短结果不截断。"""
    assert _truncate_tool_result("abc") == "abc"


def test_truncate_result_exact_limit():
    """刚好等于上限不截断。"""
    s = "x" * 3000
    assert _truncate_tool_result(s) == s


def test_truncate_result_over_limit():
    """超上限截断 + 标注原始长度。"""
    s = "x" * 5000
    out = _truncate_tool_result(s)
    assert len(out) < len(s)
    assert "5000" in out
    assert "已截断" in out
    assert out.startswith("xxx")


def test_truncate_result_custom_limit():
    """自定义上限。"""
    out = _truncate_tool_result("abcdef", max_chars=3)
    assert out.startswith("abc")
    assert "6" in out


def test_truncate_result_non_string():
    """非字符串先转 str 再截断。"""
    out = _truncate_tool_result(12345)
    assert out == "12345"


# ============ 会话级并发锁(OpenClaw session lane) ============


def test_session_lock_same_session():
    """同一 session_id 返回同一把锁。"""
    lock1 = _get_session_lock("userA")
    lock2 = _get_session_lock("userA")
    assert lock1 is lock2


def test_session_lock_different_session():
    """不同 session_id 返回不同锁。"""
    lock1 = _get_session_lock("userA")
    lock2 = _get_session_lock("userB")
    assert lock1 is not lock2


def test_session_lock_serializes():
    """锁能真正串行化并发执行。"""
    lock = _get_session_lock("test_serialize")
    order = []

    def worker(name):
        with lock:
            order.append(f"{name}-start")
            time.sleep(0.05)
            order.append(f"{name}-end")

    threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 严格交替: 0-start, 0-end, 1-start, 1-end, 2-start, 2-end (顺序可能变,但 start/end 必相邻)
    for i in range(0, len(order), 2):
        assert "-start" in order[i]
        assert "-end" in order[i + 1]
        # 同一 worker 的 start 紧跟 end
        assert order[i].split("-")[0] == order[i + 1].split("-")[0]


def test_pending_images_thread_local():
    """不同线程的图片队列互相隔离(并发会话不串号)。"""
    feishu_bot._pending_images.clear()
    results = {}

    def worker(val):
        feishu_bot._pending_images.clear()
        feishu_bot._pending_images.append(val)
        results[val] = list(feishu_bot._pending_images)

    t1 = threading.Thread(target=worker, args=("IMG_A",))
    t2 = threading.Thread(target=worker, args=("IMG_B",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results == {"IMG_A": ["IMG_A"], "IMG_B": ["IMG_B"]}
    # 主线程队列不受影响
    assert list(feishu_bot._pending_images) == []


def test_current_session_id_thread_local():
    """不同线程的 session_id 互相隔离。"""
    feishu_bot._set_current_session_id("main-session")
    results = {}

    def worker(sid):
        feishu_bot._set_current_session_id(sid)
        results[sid] = feishu_bot._current_session_id()

    t1 = threading.Thread(target=worker, args=("sessA",))
    t2 = threading.Thread(target=worker, args=("sessB",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results == {"sessA": "sessA", "sessB": "sessB"}
    # 主线程 session 不变
    assert feishu_bot._current_session_id() == "main-session"


def test_prune_idle_session_locks():
    """空闲锁应被清理,持有中的锁不受影响。"""
    guard = feishu_bot._session_locks_guard
    with guard:
        orig = feishu_bot._session_locks
        held = threading.Lock()
        held.acquire()  # 模拟持有中
        feishu_bot._session_locks = {
            "held": held,
            "idle1": threading.Lock(),
            "idle2": threading.Lock(),
        }
        feishu_bot.MAX_SESSION_LOCKS = 1  # 触发清理到只剩 1 个
        try:
            feishu_bot._prune_idle_session_locks()
            assert "held" in feishu_bot._session_locks  # 持有中的保留
            assert len(feishu_bot._session_locks) == 1
        finally:
            feishu_bot.MAX_SESSION_LOCKS = 200
            feishu_bot._session_locks = orig
            held.release()


# ============ ReAct 图片清理(多步只保留最后一轮) ============


def test_react_clears_images_between_steps(monkeypatch):
    """ReAct 循环中 LLM 猜错代码再纠正时,只保留最后一轮的图片。

    模拟场景: 用户问"正弦电气呢",
      step 1: LLM 调 analyze_stock(301395) ← 猜错
      step 2: LLM 纠正调 analyze_stock(688395) ← 正确
      step 3: LLM 给出最终文本
    只应返回 1 张图(688395),不含错误代码 301395 的图。
    """
    import httpx

    # 构造 mock LLM 响应序列
    class FakeResp:
        def __init__(self, data):
            self.status_code = 200
            self._data = data
        def json(self):
            return self._data

    class FakeClient:
        responses = []
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def post(self, *a, **kw):
            return FakeResp(FakeClient.responses.pop(0))

    FakeClient.responses = [
        # Step 1: LLM 调 analyze_stock(错误代码 301395)
        {"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "analyze_stock", "arguments": '{"code": "301395"}'}}
        ]}}]},
        # Step 2: LLM 纠正为 688395
        {"choices": [{"message": {"tool_calls": [
            {"id": "c2", "function": {"name": "analyze_stock", "arguments": '{"code": "688395"}'}}
        ]}}]},
        # Step 3: LLM 给出最终答案
        {"choices": [{"message": {"content": "正弦电气(688395)分析完成"}}]},
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    # mock analyze_stock: 每次追加一张"图"(用 bytes 标记代码)
    def fake_analyze(args):
        code = args.get("code", "")
        feishu_bot._pending_images.append(f"IMG_{code}".encode())
        return f"分析结果 {code}"

    monkeypatch.setitem(feishu_bot.TOOL_HANDLERS, "analyze_stock", lambda args: fake_analyze(args))

    # 构造 Agent(绕过 __init__ 的 .env 依赖)
    agent = feishu_bot.FeishuAgent.__new__(feishu_bot.FeishuAgent)
    agent.api_key = "fake"
    agent.base_url = "http://fake"
    agent.model = "fake"

    reply, _history, images = agent.chat("正弦电气呢", session_id="test")
    assert "分析完成" in reply
    # 关键: 只保留最后一轮(688395)的图,错误代码 301395 的图被清掉
    assert len(images) == 1
    assert images == [b"IMG_688395"]


def test_react_keeps_parallel_images(monkeypatch):
    """同一轮多个工具调用的图片都应保留(如同时分析茅台+看市场)。"""
    import httpx

    class FakeResp:
        def __init__(self, data):
            self.status_code = 200
            self._data = data
        def json(self):
            return self._data

    class FakeClient:
        responses = []
        def __init__(self, *a, **kw):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def post(self, *a, **kw):
            return FakeResp(FakeClient.responses.pop(0))

    FakeClient.responses = [
        # Step 1: LLM 同时调 analyze_stock + get_market_status(并行)
        {"choices": [{"message": {"tool_calls": [
            {"id": "c1", "function": {"name": "analyze_stock", "arguments": '{"code": "600519"}'}},
            {"id": "c2", "function": {"name": "get_market_status", "arguments": '{}'}}
        ]}}]},
        # Step 2: 最终答案
        {"choices": [{"message": {"content": "茅台+市场分析完成"}}]},
    ]
    monkeypatch.setattr(httpx, "Client", FakeClient)

    def fake_analyze(args):
        feishu_bot._pending_images.append(b"KLINE_600519")
        return "茅台分析"
    def fake_market(args):
        feishu_bot._pending_images.append(b"MARKET_CHART")
        return "市场概况"

    monkeypatch.setitem(feishu_bot.TOOL_HANDLERS, "analyze_stock", lambda args: fake_analyze(args))
    monkeypatch.setitem(feishu_bot.TOOL_HANDLERS, "get_market_status", lambda args: fake_market(args))

    agent = feishu_bot.FeishuAgent.__new__(feishu_bot.FeishuAgent)
    agent.api_key = "fake"
    agent.base_url = "http://fake"
    agent.model = "fake"

    reply, _history, images = agent.chat("分析茅台和市场", session_id="test")
    assert "完成" in reply
    # 同一轮两个工具的图都保留
    assert len(images) == 2
    assert b"KLINE_600519" in images
    assert b"MARKET_CHART" in images


# ============ 结构化工具调用日志(JSONL) ============


def test_log_tool_call_writes_jsonl(tmp_path, monkeypatch):
    """工具调用日志应写到 JSONL 文件,每行一条 JSON。"""
    log_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(feishu_bot, "TOOL_AUDIT_LOG", log_file)

    _log_tool_call("sessA", 1, "analyze_stock", {"code": "600519"},
                   500, 123, error=None)
    _log_tool_call("sessA", 2, "get_market_status", {},
                   800, 45, error="exec: network")

    lines = log_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    r1 = json.loads(lines[0])
    assert r1["session_id"] == "sessA"
    assert r1["step"] == 1
    assert r1["tool"] == "analyze_stock"
    assert r1["args"] == {"code": "600519"}
    assert r1["result_size"] == 500
    assert r1["duration_ms"] == 123
    assert r1["error"] is None
    assert "ts" in r1 and "pid" in r1

    r2 = json.loads(lines[1])
    assert r2["error"] == "exec: network"


def test_log_tool_call_failure_silent(tmp_path, monkeypatch):
    """日志写入失败不应抛异常(不影响主流程)。"""
    # 指向一个不存在的目录(不可写)
    monkeypatch.setattr(feishu_bot, "TOOL_AUDIT_LOG", Path("/nonexistent_dir/audit.jsonl"))
    # 不应抛
    _log_tool_call("s", 1, "x", {}, 0, 0, error=None)


# ============ 历史压缩(Compaction) ============


def _make_agent_with_mock_llm(summary_text: str):
    """构造 FeishuAgent,但 _summarize_with_llm 被 mock。"""
    agent = feishu_bot.FeishuAgent.__new__(feishu_bot.FeishuAgent)
    agent.api_key = "fake"
    agent.base_url = "http://fake"
    agent.model = "fake"
    agent._summarize_with_llm = lambda text, max_tokens=300: summary_text
    return agent


def test_compact_below_threshold():
    """历史条数 < COMPACTION_THRESHOLD 不压缩。"""
    agent = _make_agent_with_mock_llm("摘要")
    history = [{"role": "user", "content": f"Q{i}"} for i in range(5)]
    out = agent._compact_history(history)
    assert out is history  # 原样返回


def test_compact_above_threshold():
    """历史 >= COMPACTION_THRESHOLD 触发压缩,旧消息变 1 条摘要。"""
    agent = _make_agent_with_mock_llm("用户问了茅台,分析了玉姐评分7分。")
    history = []
    for i in range(feishu_bot.COMPACTION_THRESHOLD + 2):
        history.append({"role": "user", "content": f"Q{i}"})
        history.append({"role": "assistant", "content": f"A{i}"})
    # history 长度 = (THRESHOLD+2)*2 = 24
    out = agent._compact_history(history)
    # 期望: 1 条摘要 + COMPACTION_KEEP_RECENT 条原文
    assert len(out) == 1 + feishu_bot.COMPACTION_KEEP_RECENT
    assert "[历史摘要]" in out[0]["content"]
    assert "茅台" in out[0]["content"]
    # 最近的几条原文保留
    assert out[-1]["content"] == f"A{feishu_bot.COMPACTION_THRESHOLD + 1}"


def test_compact_llm_failure_degrades():
    """LLM 摘要失败时应降级返回原 history(由 _truncate_history 兜底)。"""
    agent = feishu_bot.FeishuAgent.__new__(feishu_bot.FeishuAgent)
    agent.api_key = "fake"
    agent.base_url = "http://fake"
    agent.model = "fake"

    def raise_fn(text, max_tokens=300):
        raise RuntimeError("LLM down")
    agent._summarize_with_llm = raise_fn

    history = [{"role": "user", "content": f"Q{i}"} for i in range(20)]
    out = agent._compact_history(history)
    assert out is history  # 原样返回,不抛


def test_compact_empty_summary_degrades():
    """LLM 返空字符串时降级。"""
    agent = _make_agent_with_mock_llm("")
    history = [{"role": "user", "content": f"Q{i}"} for i in range(20)]
    out = agent._compact_history(history)
    assert out is history


# ============ 多股票对比 compare_stocks ============


def test_compare_stocks_empty():
    """空列表应返错误。"""
    r = handler_compare_stocks([])
    assert "❌" in r


def test_compare_stocks_not_list():
    """非 list 应返错误。"""
    r = handler_compare_stocks("600519")
    assert "❌" in r


def test_compare_stocks_too_many():
    """超过 8 只应返错误。"""
    r = handler_compare_stocks(["600519"] * 10)
    assert "❌" in r or "8" in r


def test_compare_stocks_invalid_codes():
    """全部代码无效应返错误。"""
    r = handler_compare_stocks(["不存在的xxx", "也存在的yyy"])
    assert "❌" in r


def test_compare_stocks_with_mock_finance(monkeypatch):
    """mock fetch_finance 后,对比表应正常输出。"""
    import stock_finance
    def fake_fetch(code):
        return {
            "code": code,
            "name": f"股票{code}",
            "pe_ttm": 20.0,
            "pb": 5.0,
            "total_mv": 1.5e11,
            "roe": 15.0,
            "net_margin": 30.0,
            "report_name": "2026中报",
        }
    monkeypatch.setattr(stock_finance, "fetch_finance", fake_fetch)
    r = handler_compare_stocks(["600519", "000858"])
    assert "股票600519" in r
    assert "股票000858" in r
    assert "20.00" in r  # PE
    assert "|" in r  # 表格


def test_compare_stocks_string_total_mv(monkeypatch):
    """total_mv 为 '-' 字符串时不应崩溃(回归 #2),应显示 '-'。"""
    import stock_finance

    def fake_fetch(code):
        return {
            "code": code,
            "name": f"股{code}",
            "pe_ttm": 20.0,
            "pb": 5.0,
            "total_mv": "-",  # 东财对部分股票返回 '-'
            "roe": 15.0,
            "net_margin": 30.0,
            "report_name": "2026中报",
        }
    monkeypatch.setattr(stock_finance, "fetch_finance", fake_fetch)
    r = handler_compare_stocks(["600519", "000858"])
    assert "股600519" in r
    assert "| - |" in r or "-" in r  # 市值显示 '-' 而非崩溃
    assert "20.00" in r


# ============ 板块分析 analyze_sector ============


def test_analyze_sector_known(monkeypatch):
    """已知板块(fallback 路径,东财接口被 mock 为空)应返回成分股对比。"""
    import stock_finance

    def fake_fetch(code):
        return {"code": code, "name": f"股{code}", "pe_ttm": 10.0, "pb": 1.0,
                "total_mv": 1e10, "roe": 5.0, "net_margin": 10.0, "report_name": "2026中报"}

    monkeypatch.setattr(stock_finance, "fetch_finance", fake_fetch)
    # mock 东财板块索引返回空(强制走 fallback _SECTOR_MEMBERS)
    monkeypatch.setattr(feishu_bot, "_fetch_sector_index", lambda: {})
    r = handler_analyze_sector("白酒")
    assert "600519" in r  # 茅台代码在白酒板块成员里


def test_analyze_sector_dynamic(monkeypatch):
    """东财动态查询:mock 板块索引返回 {'白酒': 'BK0896'},mock 成分股返回 8 个代码。"""
    import stock_finance

    def fake_fetch(code):
        return {"code": code, "name": f"股{code}", "pe_ttm": 10.0, "pb": 1.0,
                "total_mv": 1e10, "roe": 5.0, "net_margin": 10.0, "report_name": "2026中报"}

    monkeypatch.setattr(stock_finance, "fetch_finance", fake_fetch)
    monkeypatch.setattr(feishu_bot, "_fetch_sector_index",
                        lambda: {"白酒": "BK0896", "银行": "BK1283"})
    monkeypatch.setattr(feishu_bot, "_fetch_sector_members",
                        lambda bk, top_n=8: ["600519", "000858", "000568"][:top_n])
    r = handler_analyze_sector("白酒")
    assert "白酒" in r
    assert "600519" in r
    assert "成交额" in r  # 动态查询的标题里有"按成交额排序"


def test_analyze_sector_dynamic_fuzzy(monkeypatch):
    """动态查询模糊匹配: '银' 应匹配到 '银行'。"""
    import stock_finance

    monkeypatch.setattr(stock_finance, "fetch_finance",
                        lambda code: {"code": code, "name": f"股{code}", "pe_ttm": 10.0,
                                      "pb": 1.0, "total_mv": 1e10, "roe": 5.0,
                                      "net_margin": 10.0, "report_name": "2026中报"})
    monkeypatch.setattr(feishu_bot, "_fetch_sector_index",
                        lambda: {"银行": "BK1283"})
    monkeypatch.setattr(feishu_bot, "_fetch_sector_members",
                        lambda bk, top_n=8: ["601398", "601939"])
    r = handler_analyze_sector("银")
    assert "银行" in r
    assert "601398" in r


def test_analyze_sector_dynamic_fail_fallback(monkeypatch):
    """东财动态查询失败(成分股返回空)应 fallback 到 _SECTOR_MEMBERS。"""
    import stock_finance

    monkeypatch.setattr(stock_finance, "fetch_finance",
                        lambda code: {"code": code, "name": f"股{code}", "pe_ttm": 10.0,
                                      "pb": 1.0, "total_mv": 1e10, "roe": 5.0,
                                      "net_margin": 10.0, "report_name": "2026中报"})
    # 板块索引有"白酒"但成分股请求失败
    monkeypatch.setattr(feishu_bot, "_fetch_sector_index",
                        lambda: {"白酒": "BK0896"})
    monkeypatch.setattr(feishu_bot, "_fetch_sector_members",
                        lambda bk, top_n=8: [])
    r = handler_analyze_sector("白酒")
    # 应 fallback 到硬编码白酒成员,包含 600519
    assert "600519" in r


def test_analyze_sector_unknown(monkeypatch):
    """未知板块应提示已知列表。"""
    monkeypatch.setattr(feishu_bot, "_fetch_sector_index", lambda: {"白酒": "BK0896"})
    r = handler_analyze_sector("不存在的板块xyz")
    assert "❌" in r
    assert "白酒" in r  # 列出已知


def test_analyze_sector_empty():
    """空板块名应返错误。"""
    r = handler_analyze_sector("")
    assert "❌" in r


def test_fetch_sector_index_fail_cooldown(monkeypatch):
    """东财板块索引失败后,5 分钟内不再重试。"""
    import urllib.request

    call_count = {"n": 0}

    def fake_urlopen(*a, **kw):
        call_count["n"] += 1
        raise Exception("mock 502")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # 清空缓存 + 重置失败时间
    monkeypatch.setattr(feishu_bot, "_SECTOR_INDEX_CACHE", {})
    monkeypatch.setattr(feishu_bot, "_SECTOR_INDEX_FAIL_TS", 0.0)
    # 第一次调用:会真请求并失败
    r1 = feishu_bot._fetch_sector_index()
    assert r1 == {}
    assert call_count["n"] > 0
    n1 = call_count["n"]
    # 第二次调用:冷却期内,不应再请求
    r2 = feishu_bot._fetch_sector_index()
    assert r2 == {}
    assert call_count["n"] == n1  # 没有新请求


# ============ 历史复盘 query_history_picks ============


def test_normalize_date_formats():
    """各种日期格式应规范化为 YYYYMMDD。"""
    assert _normalize_date("20260819") == "20260819"
    assert _normalize_date("2026-08-19") == "20260819"
    assert _normalize_date("2026/08/19") == "20260819"
    assert _normalize_date("2026.08.19") == "20260819"
    assert _normalize_date("xyz") == ""
    assert _normalize_date("") == feishu_bot.datetime.now().strftime("%Y%m%d")


def test_normalize_date_relative():
    """相对日期应正确解析(自动回退周末)。"""
    from datetime import datetime
    assert _normalize_date("今天") == datetime.now().strftime("%Y%m%d")
    assert _normalize_date("昨日") == _rollback_to_weekday(1).strftime("%Y%m%d")
    assert _normalize_date("前天") == _rollback_to_weekday(2).strftime("%Y%m%d")
    assert _normalize_date("大前天") == _rollback_to_weekday(3).strftime("%Y%m%d")


def _fake_now(year, month, day, hour=10):
    """构造固定当前时间,用于测周末回退。"""
    from datetime import datetime
    return datetime(year, month, day, hour, 0, 0)


class _MockDateTime:
    """模拟 datetime 类,只实现 now() 供测试注入到 feishu_bot.datetime。"""
    _fixed = None

    @classmethod
    def now(cls, *a, **k):
        return cls._fixed


def test_rollback_weekday_monday(monkeypatch):
    """周一问昨天=周日,应回退到周五(最近交易日)。"""
    _MockDateTime._fixed = _fake_now(2026, 8, 17)  # 2026-08-17 是周一
    monkeypatch.setattr(feishu_bot, "datetime", _MockDateTime)
    # 昨天=08-16 周日 → 回退到 08-14 周五
    assert _rollback_to_weekday(1).strftime("%Y%m%d") == "20260814"


def test_rollback_weekday_saturday(monkeypatch):
    """周六问昨天=周五,不需回退。"""
    _MockDateTime._fixed = _fake_now(2026, 8, 15)  # 2026-08-15 是周六
    monkeypatch.setattr(feishu_bot, "datetime", _MockDateTime)
    # 昨天=08-14 周五,直接返回
    assert _rollback_to_weekday(1).strftime("%Y%m%d") == "20260814"


def test_rollback_weekday_weekday(monkeypatch):
    """普通工作日不影响。"""
    _MockDateTime._fixed = _fake_now(2026, 8, 19)  # 2026-08-19 是周三
    monkeypatch.setattr(feishu_bot, "datetime", _MockDateTime)
    assert _rollback_to_weekday(1).strftime("%Y%m%d") == "20260818"


def test_query_history_picks_invalid_date():
    """无效日期格式应返错误。"""
    r = handler_query_history_picks("xyz")
    assert "❌" in r


def test_query_history_picks_no_data(tmp_path, monkeypatch):
    """指定日期无数据应提示并显示可用日期。"""
    # 用临时 db 避免影响真实数据
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE yujie_picks (date TEXT, rank INTEGER, code TEXT, name TEXT, score REAL, hits TEXT, detail TEXT)")
    conn.execute("INSERT INTO yujie_picks VALUES(?,?,?,?,?,?,?)", ("20260820", 1, "600519", "茅台", 7.0, "[]", "{}"))
    conn.commit()
    conn.close()

    import yujie_scan
    monkeypatch.setattr(yujie_scan, "CACHE_DB", str(db))
    monkeypatch.setattr(feishu_bot, "ENGINE_HOME", tmp_path)
    # 让 stock_cache.db 实际指到临时 db
    (tmp_path / "stock_cache.db").symlink_to(db) if False else None
    # 直接复制一份到 stock_cache.db 名字
    import shutil
    shutil.copy(str(db), str(tmp_path / "stock_cache.db"))

    r = handler_query_history_picks("20260101")
    assert "❌" in r
    assert "20260101" in r


def test_query_history_picks_with_data(tmp_path, monkeypatch):
    """指定日期有数据应返回精简列表(显示前 5,6-10 汇总一行)。"""
    db = tmp_path / "stock_cache.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE yujie_picks (date TEXT, rank INTEGER, code TEXT, name TEXT, score REAL, hits TEXT, detail TEXT)")
    for i in range(15):
        conn.execute("INSERT INTO yujie_picks VALUES(?,?,?,?,?,?,?)",
                     ("20260819", i + 1, f"600{i:03d}", f"股票{i}", 7.0 - i * 0.3, '[]', "{}"))
    conn.commit()
    conn.close()

    import yujie_scan
    monkeypatch.setattr(yujie_scan, "CACHE_DB", str(db))
    monkeypatch.setattr(feishu_bot, "ENGINE_HOME", tmp_path)

    r = handler_query_history_picks("20260819")
    assert "20260819" in r
    assert "显示前 5" in r or "共 15" in r  # 精简显示
    assert "评分" in r  # 评分分布(精简版)
    assert "共 15" in r  # 总共 15 只


# ============ Bot 运行状态统计 ============


def test_stats_incr_basic():
    """_incr_stats 累加计数器。"""
    before = feishu_bot._STATS.get("tool_calls", 0)
    _incr_stats("tool_calls", 1)
    after = feishu_bot._STATS.get("tool_calls", 0)
    assert after == before + 1


def test_stats_incr_multiple():
    """多次累加。"""
    before = feishu_bot._STATS.get("llm_calls", 0)
    for _ in range(5):
        _incr_stats("llm_calls", 1)
    after = feishu_bot._STATS.get("llm_calls", 0)
    assert after == before + 5


def test_stats_add_session():
    """会话 ID 应去重存入 set。"""
    before = len(feishu_bot._STATS["sessions"])
    _stats_add_session("test_sess_A")
    _stats_add_session("test_sess_A")  # 重复不应增加
    _stats_add_session("test_sess_B")
    after = len(feishu_bot._STATS["sessions"])
    assert after == before + 2  # A、B 两个新会话


def test_stats_add_session_cap(monkeypatch):
    """会话数达到上限后不再增长(防无界内存)。"""
    with feishu_bot._STATS_LOCK:
        orig = feishu_bot._STATS["sessions"]
    feishu_bot._STATS["sessions"] = set()
    try:
        # 填满上限
        n = feishu_bot.MAX_TRACKED_SESSIONS + 50
        for i in range(n):
            _stats_add_session(f"cap_sess_{i}")
        assert len(feishu_bot._STATS["sessions"]) == feishu_bot.MAX_TRACKED_SESSIONS
    finally:
        feishu_bot._STATS["sessions"] = orig


def test_log_tool_call_increments_stats(tmp_path, monkeypatch):
    """_log_tool_call 应同步累加 tool_calls / tool_failures / sessions。"""
    log_file = tmp_path / "audit.jsonl"
    monkeypatch.setattr(feishu_bot, "TOOL_AUDIT_LOG", log_file)
    tc_before = feishu_bot._STATS.get("tool_calls", 0)
    tf_before = feishu_bot._STATS.get("tool_failures", 0)
    sess_before = len(feishu_bot._STATS["sessions"])

    _log_tool_call("test_stats_sess", 1, "analyze_stock", {"code": "600519"},
                   200, 100, error=None)
    _log_tool_call("test_stats_sess", 2, "backtest_strategy", {},
                   0, 50, error="timeout")

    assert feishu_bot._STATS["tool_calls"] == tc_before + 2
    assert feishu_bot._STATS["tool_failures"] == tf_before + 1
    assert len(feishu_bot._STATS["sessions"]) == sess_before + 1  # 同 session_id 只算 1 个


def test_print_stats_no_crash(capsys):
    """_print_stats 在零数据时不应崩溃。"""
    # 临时把 _STATS 改成零数据
    orig = dict(feishu_bot._STATS)
    feishu_bot._STATS.update({
        "start_time": feishu_bot.datetime.now(),
        "llm_calls": 0, "llm_total_ms": 0, "llm_failures": 0,
        "tool_calls": 0, "tool_failures": 0, "sessions": set(),
    })
    try:
        _print_stats()  # 不应抛异常
    finally:
        feishu_bot._STATS.update(orig)


def test_print_stats_with_data(capsys):
    """有数据时 _print_stats 应输出完整摘要。"""
    orig = dict(feishu_bot._STATS)
    feishu_bot._STATS.update({
        "start_time": feishu_bot.datetime.now(),
        "llm_calls": 10, "llm_total_ms": 5000, "llm_failures": 1,
        "tool_calls": 20, "tool_failures": 2, "sessions": {"a", "b", "c"},
    })
    try:
        with patch("feishu_bot.log") as mock_log:
            _print_stats()
            assert mock_log.info.called
            # log.info 用 %-格式化,call_args[0] 是 (template, *args)
            template = mock_log.info.call_args[0][0]
            fmt_args = mock_log.info.call_args[0][1:]
            rendered = template % fmt_args
            assert "LLM 调用 10 次" in rendered
            assert "工具调用 20 次" in rendered
            assert "累计会话 3 个" in rendered
    finally:
        feishu_bot._STATS.update(orig)






# ---------------- 消息去重 _is_duplicate_message ----------------


def test_is_duplicate_message_first_time_false():
    """第一次见到的 msg_id 应返回 False(未处理过)。"""
    import feishu_bot
    # 清空状态
    feishu_bot._seen_message_ids.clear()
    assert feishu_bot._is_duplicate_message("om_test_001") is False


def test_is_duplicate_message_second_time_true():
    """同一 msg_id 第二次应返回 True(已处理过)。"""
    import feishu_bot
    feishu_bot._seen_message_ids.clear()
    feishu_bot._is_duplicate_message("om_test_002")
    assert feishu_bot._is_duplicate_message("om_test_002") is True


def test_is_duplicate_message_different_ids():
    """不同 msg_id 都应返回 False。"""
    import feishu_bot
    feishu_bot._seen_message_ids.clear()
    assert feishu_bot._is_duplicate_message("om_a") is False
    assert feishu_bot._is_duplicate_message("om_b") is False
    assert feishu_bot._is_duplicate_message("om_c") is False
    assert feishu_bot._is_duplicate_message("om_a") is True  # 重复


def test_is_duplicate_message_lru_eviction():
    """超过 SEEN_MSG_MAX 应淘汰最老的。"""
    import feishu_bot
    feishu_bot._seen_message_ids.clear()
    # 填满到上限
    for i in range(feishu_bot.SEEN_MSG_MAX):
        feishu_bot._is_duplicate_message(f"om_{i:04d}")
    # 加一个新 id,应触发淘汰
    feishu_bot._is_duplicate_message("om_new")
    # 最老的 om_0000 应被淘汰,再插入应返回 False
    assert feishu_bot._is_duplicate_message("om_0000") is False


def test_is_duplicate_message_empty_id():
    """空 msg_id 应返回 False(不去重,允许处理)。"""
    import feishu_bot
    assert feishu_bot._is_duplicate_message("") is False
