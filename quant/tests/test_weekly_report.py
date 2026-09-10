"""每周战报单元测试。不联网(指数/行情接口 mock)。"""

import pandas as pd
import pytest

import config_store
import weekly_report


@pytest.fixture(autouse=True)
def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "WATCHLIST_DB", tmp_path / "w.db")
    monkeypatch.setattr(config_store, "CONFIG_PATH", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(
        '{"feishu": {"enabled": true, "app_id": "x", "app_secret": "y", '
        '"chat_id": "oc_test"}}',
        encoding="utf-8",
    )
    yield


def test_weekly_change(monkeypatch):
    df = pd.DataFrame({
        "code": "600519", "date": [f"202609{i:02d}" for i in range(1, 11)],
        "open": 10.0, "close": [100, 100, 100, 100, 100, 110, 110, 110, 110, 110],
        "high": 10.0, "low": 10.0, "volume": 1,
    })
    monkeypatch.setattr("data_fetcher.get_daily_data", lambda code, days=320: df)
    assert weekly_report.weekly_change("600519") == pytest.approx(0.1)


def test_weekly_change_short(monkeypatch):
    df = pd.DataFrame({
        "code": "600519", "date": ["20260901", "20260902"],
        "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1,
    })
    monkeypatch.setattr("data_fetcher.get_daily_data", lambda code, days=320: df)
    assert weekly_report.weekly_change("600519") is None


def test_build_card_structure(monkeypatch):
    monkeypatch.setattr(weekly_report, "build_index_lines", lambda: ["上证指数 3000"])
    monkeypatch.setattr(weekly_report, "build_watchlist_lines", lambda: ["🟢 茅台 +5%"])
    monkeypatch.setattr(weekly_report, "build_journal_lines", lambda: ["[09-10] 买入 茅台"])
    import signal_tracker
    monkeypatch.setattr(signal_tracker, "evaluate_pending", lambda limit=200: 0)
    monkeypatch.setattr(signal_tracker, "weekly_summary", lambda weeks=1: "📊 绩效")

    card = weekly_report.build_card()
    assert card["header"]["title"]["content"].startswith("📋 每周战报")
    assert len(card["elements"]) == 4
    contents = [el["text"]["content"] for el in card["elements"]]
    assert "大盘指数" in contents[0]
    assert "自选池本周" in contents[1]
    assert "信号绩效" in contents[2]
    assert "本周日记" in contents[3]


def test_journal_lines(monkeypatch):
    import journal
    monkeypatch.setattr(journal, "JOURNAL_DB", "/tmp/test_jr_db.db")
    import os

    if os.path.exists("/tmp/test_jr_db.db"):
        os.remove("/tmp/test_jr_db.db")
    journal.journal_add("oc_test", "buy", "600519", "贵州茅台", price=1700, qty=100, note="test")
    lines = weekly_report.build_journal_lines()
    assert any("买入" in line and "贵州茅台" in line for line in lines)


def test_run_once_dry_run(capsys):
    weekly_report.run_once(dry_run=True)
    out = capsys.readouterr().out
    assert "大盘指数" in out
