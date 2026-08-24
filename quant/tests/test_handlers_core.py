"""核心 handler 测试覆盖(第 4 轮优化 #17)。

覆盖 24 个无测试 handler 中的核心查询类(market/yujie/finance/portfolio/
list_strategies/get_yujie_detail/get_index/get_lhb/get_north_flow/
get_main_flow/get_concept_sectors/get_stock_news/get_strategy_library)。

策略: mock 底层 fetch_xxx / yujie_scan / strategy_engine,验证 handler 输出格式。
"""
import sqlite3
import unittest.mock as mock

import feishu_bot

# ============ handler_market ============


def test_handler_market_no_data(tmp_path, monkeypatch):
    """无日报数据应返回提示。"""
    monkeypatch.setattr(feishu_bot, "REPORTS_DIR", tmp_path)
    out = feishu_bot.handler_market()
    assert "尚未生成" in out or "❌" in out


def test_handler_market_with_data(tmp_path, monkeypatch):
    """有日报数据应返回市场概况。"""
    monkeypatch.setattr(feishu_bot, "REPORTS_DIR", tmp_path)
    from datetime import datetime
    today = datetime.now().strftime("%Y%m%d")
    (tmp_path / f"daily_{today}.md").write_text(
        "## 一、全市场扫描\n总成交 12000 亿,涨停 80 跌停 5\n## 二、玉姐精选\n1. 600519 茅台 8分",
        encoding="utf-8",
    )
    out = feishu_bot.handler_market()
    assert "12000" in out or "80" in out or "涨停" in out


# ============ handler_yujie ============


def test_handler_yujie_no_data(tmp_path, monkeypatch):
    """无玉姐精选数据应返回提示。"""
    monkeypatch.setattr(feishu_bot, "ENGINE_HOME", tmp_path)
    import yujie_scan
    monkeypatch.setattr(yujie_scan, "CACHE_DB", str(tmp_path / "stock_cache.db"))
    out = feishu_bot.handler_yujie()
    assert "尚未生成" in out or "❌" in out or "无" in out


def test_handler_yujie_with_data(tmp_path, monkeypatch):
    """有玉姐精选数据应返回 Top 列表。"""
    monkeypatch.setattr(feishu_bot, "ENGINE_HOME", tmp_path)
    db = tmp_path / "stock_cache.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE yujie_picks(
        date TEXT, rank INTEGER, code TEXT, name TEXT,
        score REAL, hits TEXT, detail TEXT)""")
    conn.execute("INSERT INTO yujie_picks VALUES(?,?,?,?,?,?,?)",
                 ("20260824", 1, "600519", "茅台", 8.0,
                  '["MACD金叉","突破"]', "{}"))
    conn.commit()
    conn.close()
    import yujie_scan
    monkeypatch.setattr(yujie_scan, "CACHE_DB", str(db))
    out = feishu_bot.handler_yujie()
    assert "600519" in out or "茅台" in out


# ============ handler_portfolio ============


def test_handler_portfolio_no_db(tmp_path, monkeypatch):
    """无持仓 db 应返回提示。"""
    monkeypatch.setattr(feishu_bot, "AGENT_DB", tmp_path / "nonexistent.db")
    out = feishu_bot.handler_portfolio()
    assert "暂无" in out or "📭" in out


def test_handler_portfolio_with_data(tmp_path, monkeypatch):
    """有持仓数据应返回持仓列表。"""
    db = tmp_path / "agent_data.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE positions(
        code TEXT, qty INTEGER, cost REAL, buy_date TEXT)""")
    conn.execute("INSERT INTO positions VALUES(?,?,?,?)",
                 ("600519", 100, 1500.0, "20260101"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(feishu_bot, "AGENT_DB", db)
    out = feishu_bot.handler_portfolio()
    assert "600519" in out


# ============ handler_finance ============


def test_handler_finance_invalid_code():
    """非法代码应返回错误。"""
    out = feishu_bot.handler_finance("abc")
    assert "❌" in out or "6 位" in out


def test_handler_finance_valid_code(monkeypatch):
    """有效代码应返回财务数据格式化。"""
    fake_data = {
        "code": "600519", "name": "贵州茅台",
        "pe_ttm": 20.0, "pb": 6.0, "total_mv": 1.88e11,
        "roe": 16.75, "gross_margin": 89.56, "eps": 35.57,
    }
    monkeypatch.setattr("stock_finance.fetch_finance", lambda c: fake_data)
    out = feishu_bot.handler_finance("600519")
    assert "贵州茅台" in out or "600519" in out
    assert "20" in out  # PE


# ============ handler_list_strategies ============


def test_handler_list_strategies():
    """应返回策略列表。"""
    out = feishu_bot.handler_list_strategies()
    # 应包含 macd 或 boll 等内置策略名
    assert "macd" in out.lower() or "MACD" in out or "策略" in out


# ============ handler_get_yujie_detail ============


def test_handler_get_yujie_detail():
    """应返回玉姐 10 条评分规则。"""
    out = feishu_bot.handler_get_yujie_detail()
    assert "玉姐" in out
    # 应包含规则名(MACD金叉 或 低位区 等)
    assert "MACD" in out or "金叉" in out or "低位" in out


# ============ handler_get_index ============


def test_handler_get_index(monkeypatch):
    """应返回指数行情。"""
    fake = [{"name": "上证指数", "code": "000001", "price": 3905.2,
             "pct": 0.04, "change": 1.48, "amount": 8.83e11}]
    monkeypatch.setattr("stock_market_extras.fetch_index", lambda name=None: fake)
    out = feishu_bot.handler_get_index()
    assert "上证指数" in out
    assert "3905" in out


def test_handler_get_index_error(monkeypatch):
    """接口错误应友好提示。"""
    monkeypatch.setattr("stock_market_extras.fetch_index",
                        lambda name=None: (_ for _ in ()).throw(Exception("HTTP Error 502")))
    out = feishu_bot.handler_get_index()
    assert "❌" in out
    assert "不可用" in out  # _friendly_err 友好化


# ============ handler_get_lhb ============


def test_handler_get_lhb(monkeypatch):
    """龙虎榜应返回数据。"""
    fake = [{"code": "600519", "name": "茅台", "reason": "日涨幅偏离",
             "net_buy": 1.5e8, "date": "20260824"}]
    monkeypatch.setattr("stock_market_extras.fetch_lhb", lambda **k: fake)
    out = feishu_bot.handler_get_lhb()
    assert "600519" in out or "茅台" in out


# ============ handler_get_north_flow ============


def test_handler_get_north_flow(monkeypatch):
    """北向资金应返回数据。"""
    fake = [{"date": "20260824", "hk2sh_net": 5.2, "hk2sz_net": 3.1, "total_net": 8.3}]
    monkeypatch.setattr("stock_market_extras.fetch_north_flow", lambda **k: fake)
    out = feishu_bot.handler_get_north_flow()
    assert "8.3" in out or "北向" in out


# ============ handler_get_main_flow ============


def test_handler_get_main_flow(monkeypatch):
    """个股主力资金流应返回数据。"""
    fake = {"name": "茅台", "main_net": 1.5e8, "super_large_net": 2e8,
            "large_net": 1e8, "medium_net": -5e7, "small_net": -1e8}
    monkeypatch.setattr("stock_market_extras.fetch_main_flow", lambda c: fake)
    out = feishu_bot.handler_get_main_flow("600519")
    assert "茅台" in out


# ============ handler_get_concept_sectors ============


def test_handler_get_concept_sectors(monkeypatch):
    """概念板块反查应返回板块列表。"""
    fake = [{"board_name": "白酒", "board_code": "BK0477", "board_type": "行业"},
            {"board_name": "消费", "board_code": "BK0438", "board_type": "概念"}]
    monkeypatch.setattr("stock_market_extras.fetch_concept_sectors", lambda c: fake)
    out = feishu_bot.handler_get_concept_sectors("600519")
    assert "白酒" in out or "消费" in out


# ============ handler_get_stock_news ============


def test_handler_get_stock_news_invalid_code():
    """非法代码应返回错误。"""
    out = feishu_bot.handler_get_stock_news("不存在的股票xyz")
    assert "❌" in out or "无法识别" in out


def test_handler_get_stock_news_with_data(monkeypatch):
    """有新闻应返回精简列表(top 8)。"""
    monkeypatch.setattr("stock_names.resolve_code", lambda c: "600519")
    fake_news = [{"title": f"新闻{i}", "time": "2026-08-24", "source": "东财",
                  "summary": "摘要" * 20, "url": "http://x"} for i in range(15)]
    monkeypatch.setattr("news_digest.fetch_stock_news", lambda *a, **k: fake_news)
    # mock stock_names DB 查名
    monkeypatch.setattr("sqlite3.connect", lambda *a, **k: mock.MagicMock(
        execute=lambda *a, **k: [("",)], close=lambda: None))
    out = feishu_bot.handler_get_stock_news("600519")
    assert "新闻0" in out
    assert "前 8" in out or "共 15" in out


# ============ handler_get_strategy_library ============


def test_handler_get_strategy_library_default():
    """默认查询应返回策略大全概览。"""
    out = feishu_bot.handler_get_strategy_library()
    assert "策略" in out or "漫画书" in out or "操练大全" in out


def test_handler_get_strategy_library_by_source():
    """按来源过滤应返回对应策略。"""
    out = feishu_bot.handler_get_strategy_library(source="yujie_custom")
    assert "玉姐" in out or "MACD" in out


def test_handler_get_strategy_library_unimplemented():
    """implemented_only=false 应列出未实现策略(含 T+0)。"""
    out = feishu_bot.handler_get_strategy_library(implemented_only=False)
    assert "T+0" in out or "未实现" in out


# ============ handler_analyze(mock,避免联网) ============


def test_handler_analyze_invalid_code():
    """非法代码应返回错误。"""
    out = feishu_bot.handler_analyze("不存在的xyz")
    # handler_analyze 内部会调 resolve_code,失败应返错误
    assert "❌" in out or "无法" in out or "未识别" in out


def test_handler_analyze_valid_code(monkeypatch):
    """有效代码应返回分析结果(mock strategy_engine.analyze)。"""
    fake_result = {
        "code": "600519", "name": "贵州茅台", "verdict": "买入",
        "reasons": ["MACD金叉"], "realtime": {"price": 1500, "pct": 0.5},
        "indicators": {}, "signals": [], "dates": [],
    }
    monkeypatch.setattr("strategy_engine.analyze", lambda *a, **k: fake_result)
    # 也 mock 图片生成避免 matplotlib
    monkeypatch.setattr("feishu_image.gen_kline_chart", lambda *a, **k: None)
    out = feishu_bot.handler_analyze("600519")
    assert "600519" in out or "茅台" in out
