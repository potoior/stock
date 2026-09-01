"""模拟持仓工具(get_portfolio handler)+ 每日体检脚本测试。

不联网:行情接口 mock,db 用 tmp_path。
"""

import sqlite3

import feishu_bot
import watchlist_check

# ---------------- 存储层 ----------------


def _setup_db(tmp_path, monkeypatch):
    db = tmp_path / "portfolio.db"
    monkeypatch.setattr(feishu_bot, "PORTFOLIO_DB", db)
    return db


def test_buy_list_roundtrip(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1500.0, "2026-09-01")
    items = feishu_bot.portfolio_list("s1")
    assert len(items) == 1
    assert items[0]["code"] == "600519"
    assert items[0]["qty"] == 100
    assert items[0]["cost"] == 1500.0


def test_buy_weighted_average(tmp_path, monkeypatch):
    """二次买入应加权平均成本。"""
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1500.0)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1700.0)
    items = feishu_bot.portfolio_list("s1")
    assert items[0]["qty"] == 200
    assert abs(items[0]["cost"] - 1600.0) < 0.01  # (1500*100+1700*100)/200


def test_sell_partial_and_all(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1500.0)
    out = feishu_bot.portfolio_sell("s1", "600519", 40)
    assert "卖出" in out
    items = feishu_bot.portfolio_list("s1")
    assert items[0]["qty"] == 60
    out = feishu_bot.portfolio_sell("s1", "600519")  # 默认全部
    assert "清仓" in out
    assert feishu_bot.portfolio_list("s1") == []


def test_sell_not_exists(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    out = feishu_bot.portfolio_sell("s1", "600519")
    assert "没有" in out


def test_clear(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "茅台", 100, 1500.0)
    feishu_bot.portfolio_buy("s1", "000001", "平安银行", 200, 10.0)
    out = feishu_bot.portfolio_clear("s1")
    assert "2" in out
    assert feishu_bot.portfolio_list("s1") == []


def test_session_isolation(tmp_path, monkeypatch):
    """不同用户(session_id)持仓隔离。"""
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1500.0)
    feishu_bot.portfolio_buy("s2", "000001", "平安银行", 200, 10.0)
    assert len(feishu_bot.portfolio_list("s1")) == 1
    assert len(feishu_bot.portfolio_list("s2")) == 1
    assert feishu_bot.portfolio_list("s1")[0]["code"] == "600519"


# ---------------- handler 层 ----------------


def test_handler_list_empty(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    out = feishu_bot.handler_portfolio("list", session_id="s1")
    assert "无持仓" in out or "📭" in out


def test_handler_buy_and_list(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)

    # mock 实时价(买入手动指定价格,避免联网)
    out = feishu_bot.handler_portfolio(
        "buy", code="600519", qty=100, price=1500.0, session_id="s1"
    )
    assert "买入" in out
    # 再买一次,验证 handler 走加仓逻辑
    out = feishu_bot.handler_portfolio(
        "buy", code="600519", qty=100, price=1700.0, session_id="s1"
    )
    assert "买入" in out
    items = feishu_bot.portfolio_list("s1")
    assert abs(items[0]["cost"] - 1600.0) < 0.01


def test_handler_buy_no_price_uses_realtime(tmp_path, monkeypatch):
    """买入不指定价格时用实时价。"""
    _setup_db(tmp_path, monkeypatch)

    import strategy_engine as se

    def fake_fetch(codes):
        return [{"code": "600519", "name": "贵州茅台", "price": 1520.0, "pct": 1.0}]

    monkeypatch.setattr(se, "fetch_realtime", fake_fetch)
    out = feishu_bot.handler_portfolio("buy", code="600519", qty=100, session_id="s1")
    assert "1520" in out
    items = feishu_bot.portfolio_list("s1")
    assert items[0]["cost"] == 1520.0


def test_handler_buy_invalid(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    out = feishu_bot.handler_portfolio("buy", code="", qty=100, session_id="s1")
    assert "❌" in out
    out = feishu_bot.handler_portfolio("buy", code="600519", qty=0, session_id="s1")
    assert "❌" in out


def test_handler_sell_flow(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    feishu_bot.portfolio_buy("s1", "600519", "贵州茅台", 100, 1500.0)
    out = feishu_bot.handler_portfolio("sell", code="600519", qty=50, session_id="s1")
    assert "卖出" in out
    assert feishu_bot.portfolio_list("s1")[0]["qty"] == 50


def test_handler_unknown_action(tmp_path, monkeypatch):
    _setup_db(tmp_path, monkeypatch)
    out = feishu_bot.handler_portfolio("xxx", session_id="s1")
    assert "❌" in out


# ---------------- watchlist_check ----------------


def test_check_no_data(tmp_path, monkeypatch):
    """无持仓无自选 → None。"""
    monkeypatch.setattr(watchlist_check, "PORTFOLIO_DB", tmp_path / "p.db")
    monkeypatch.setattr(watchlist_check, "WATCHLIST_DB", tmp_path / "w.db")
    monkeypatch.setattr(watchlist_check, "CONFIG_PATH", tmp_path / "c.json")
    assert watchlist_check.build_card([], []) is None


def test_check_build_card(tmp_path, monkeypatch):
    """卡片构建:持仓盈亏 + 自选信号 + 卖出信号标注。"""
    monkeypatch.setattr(
        watchlist_check,
        "analyze_one",
        lambda code: {
            "name": "贵州茅台",
            "price": 1600.0,
            "pct": 1.2,
            "verdict": "观望",
            "buys": ["MACD: 金叉"],
            "sells": ["KDJ: 超买"],
        },
    )
    card = watchlist_check.build_card(
        positions=[{"code": "600519", "name": "贵州茅台", "qty": 100,
                     "cost": 1500.0, "buy_date": "2026-09-01"}],
        watchlist=[{"code": "000001", "name": "平安银行"}],
    )
    content = card["elements"][0]["text"]["content"]
    assert "持仓体检" in content
    assert "自选信号" in content
    assert "+10000元" in content  # (1600-1500)*100
    assert "⚠️" in content
    assert "平安银行" in content


def test_check_load_positions(tmp_path, monkeypatch):
    """load_chat_positions 按 chat_id 前缀过滤。"""
    db = tmp_path / "portfolio.db"
    monkeypatch.setattr(watchlist_check, "PORTFOLIO_DB", db)
    monkeypatch.setattr(
        watchlist_check, "CONFIG_PATH", tmp_path / "config.json",
    )
    (tmp_path / "config.json").write_text(
        '{"feishu": {"chat_id": "oc_123"}}', encoding="utf-8"
    )
    # 直接写表(绕过 feishu_bot 的 PORTFOLIO_DB)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE positions(session_id TEXT, code TEXT, name TEXT,"
        " qty REAL, cost REAL, buy_date TEXT, ts INTEGER,"
        " PRIMARY KEY (session_id, code))"
    )
    conn.execute("INSERT INTO positions VALUES('oc_123:u1','600519','贵州茅台',100,1500.0,'2026-09-01',0)")
    conn.execute("INSERT INTO positions VALUES('oc_456:u2','000001','平安银行',200,10.0,'2026-09-01',0)")
    conn.commit()
    conn.close()
    rows = watchlist_check.load_chat_positions()
    assert len(rows) == 1
    assert rows[0]["code"] == "600519"
