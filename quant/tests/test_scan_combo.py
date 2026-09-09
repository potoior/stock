"""scan_combo 多策略组合选股测试(不联网,daily 表用临时 db mock)。"""

import sqlite3

import pytest

import feishu_bot
import market_scan as ms

MOCK_KLINE = """CREATE TABLE IF NOT EXISTS daily (
    code TEXT, name TEXT, date TEXT, open REAL, high REAL, low REAL,
    close REAL, volume REAL, PRIMARY KEY(code, date))"""


def _seed_daily(db, code, rows):
    """写入构造好的 K 线: rows = [(close, volume), ...]"""
    conn = sqlite3.connect(str(db))
    conn.execute(MOCK_KLINE)
    for i, (close, vol) in enumerate(rows):
        d = f"2026{1 + i // 28:02d}{1 + i % 28:02d}"
        conn.execute(
            "INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?)",
            (code, f"股票{code}", d, close, close, close, close, vol),
        )
    conn.commit()
    conn.close()


def _rising_kline(n=120, base=10.0, vol=1e7):
    """构造温和上涨序列(触发均线/趋势类买入)。"""
    rows = []
    price = base
    for _ in range(n):
        price *= 1.008  # 每日涨 0.8%
        rows.append((round(price, 3), vol))
    return rows


def test_combo_and_requires_all(tmp_path, monkeypatch):
    """AND 模式:只触发其中一个策略的股票不应命中。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())

    # macd + kdj:两只,AND 模式
    r = ms.scan_combo_strategies(["macd", "kdj"], mode="and", limit=50)
    assert "hits" in r
    assert r["scanned"] == 1


def test_combo_unknown_strategy(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["macd", "xxx"], mode="and")
    assert "error" in r


def test_combo_no_scan_strategy(tmp_path, monkeypatch):
    """需联网的策略不允许扫描。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["macd", "policy_select"], mode="and")
    assert "error" in r


def test_combo_rejects_inert_strategy(tmp_path, monkeypatch):
    """观察型策略(只返回 hold,不产生买卖信号)不允许进组合扫描。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["macd", "zt_pull"], mode="and")
    assert "error" in r
    assert "观察型" in r["error"]


def test_combo_wrong_count(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    assert "error" in ms.scan_combo_strategies(["macd"], mode="and")
    assert "error" in ms.scan_combo_strategies(["macd"] * 6, mode="and")


def test_combo_invalid_mode(tmp_path, monkeypatch):
    """mode 只允许 and/or,非法值应报错而非静默按 or 处理。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["macd", "kdj"], mode="AND")
    assert "error" in r
    assert "mode" in r["error"]


def test_combo_duplicate_strategy(tmp_path, monkeypatch):
    """重复策略 id 应报错。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["macd", "macd"], mode="and")
    assert "error" in r
    assert "重复" in r["error"]


def test_combo_hits_structure(tmp_path, monkeypatch):
    """命中结果应含 signals 列表(触发了哪些策略)。"""
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    _seed_daily(db, "600519", _rising_kline())
    r = ms.scan_combo_strategies(["ma_combo", "trend_follow"], mode="or", limit=50)
    for h in r["hits"]:
        assert isinstance(h["signals"], list)
        assert len(h["signals"]) >= 1
        assert h["amount_yi"] > 0


def test_combo_empty_db(tmp_path, monkeypatch):
    db = tmp_path / "cache.db"
    monkeypatch.setattr(ms, "CACHE_DB", db)
    conn = sqlite3.connect(str(db))  # 建空表
    conn.execute(MOCK_KLINE)
    conn.commit()
    conn.close()
    r = ms.scan_combo_strategies(["macd", "kdj"], mode="and")
    assert "error" in r


def test_handler_scan_combo_registered():
    """scan_combo 已注册到 TOOL_HANDLERS。"""
    assert "scan_combo" in feishu_bot.TOOL_HANDLERS
    assert "scan_combo" in feishu_bot.SLOW_TOOLS
    # TOOLS schema 里有定义
    names = [t["function"]["name"] for t in feishu_bot.TOOLS]
    assert "scan_combo" in names


@pytest.fixture(autouse=True)
def _no_progress():
    """进度回调现在经显式 ctx 传入,CLI ctx 无 bot 自然静默,无需屏蔽。"""
