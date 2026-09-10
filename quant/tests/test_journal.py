"""交易日记单元测试。不联网(实时价接口 mock)。"""

import pytest

import journal


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DB", tmp_path / "j.db")
    yield


def test_journal_add_and_list():
    assert "已记录买入" in journal.journal_add("s1", "buy", "600519", "贵州茅台",
                                                price=1700, qty=100, note="龙回头")
    journal.journal_add("s1", "note", "600519", "贵州茅台", note="观察量能")
    rows = journal.journal_rows("s1")
    assert len(rows) == 2
    assert rows[0]["action"] == "note"  # 倒序,最新在前

    # 会话隔离
    assert journal.journal_rows("s2") == []

    text = journal.journal_list("s1")
    assert "贵州茅台" in text and "买入" in text


def test_journal_list_filters():
    journal.journal_add("s1", "buy", "600519", "贵州茅台", price=1700)
    journal.journal_add("s1", "buy", "000001", "平安银行", price=12)
    # code 过滤
    rows = journal.journal_rows("s1", code="600519")
    assert len(rows) == 1 and rows[0]["code"] == "600519"
    # days=1 应都在
    assert len(journal.journal_rows("s1", days=1)) == 2


def test_journal_list_pnl(monkeypatch):
    journal.journal_add("s1", "buy", "600519", "贵州茅台", price=1700, qty=100)
    monkeypatch.setattr(
        "data_fetcher.fetch_realtime",
        lambda codes: [{"code": "600519", "name": "贵州茅台", "price": 1800, "pct": 1.0}],
    )
    text = journal.journal_list("s1")
    assert "+5.9%" in text  # 1700 -> 1800


def test_journal_empty():
    assert "没有找到" in journal.journal_list("s1")
