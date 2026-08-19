"""feishu_bot 新功能单测: 历史裁剪/分段、自选股、股票名识别。

不联网,所有外部依赖 mock 或用临时 db。
"""

import json
import sqlite3
import time
from unittest.mock import patch

import feishu_bot
import stock_names
from feishu_bot import (
    _is_reset_command,
    _split_long_text,
    _truncate_history,
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
