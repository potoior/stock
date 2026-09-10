"""信号绩效跟踪单元测试。不联网(K 线接口 mock)。"""

import time

import pandas as pd
import pytest

import signal_tracker


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(signal_tracker, "SIGNALS_DB", tmp_path / "s.db")
    yield


def _result(buys=(), sells=()):
    return {
        "buy_reasons": [{"name": n, "reason": "r"} for n in buys],
        "sell_reasons": [{"name": n, "reason": "r"} for n in sells],
        "realtime": {"name": "贵州茅台", "price": 1700},
    }


def test_record_dedup():
    assert signal_tracker.record_signals("600519", _result(buys=["MACD"])) == 1
    assert signal_tracker.record_signals("600519", _result(buys=["MACD"])) == 0  # 同日去重
    assert signal_tracker.record_signals("600519", _result(buys=["KDJ"])) == 1
    assert signal_tracker.record_signals("600519", _result(sells=["MACD"])) == 1  # 方向不同不去重


def test_record_empty():
    assert signal_tracker.record_signals("600519", _result()) == 0


def test_evaluate_pending(monkeypatch):
    signal_tracker.record_signals("600519", _result(buys=["MACD"]))

    # 10 个交易日 K 线(以今天=信号日开头): 第 5 日收 110(+10%),第 20 日不存在
    dates = pd.date_range(pd.Timestamp.now().normalize() - pd.Timedelta(days=0),
                          periods=10, freq="B").strftime("%Y%m%d")
    df = pd.DataFrame({
        "code": "600519", "date": dates,
        "open": 100.0, "close": [100] * 3 + [110] * 7,  # 前 3 日 100,后面 110
        "high": 101.0, "low": 99.0, "volume": 1,
    })
    monkeypatch.setattr("data_fetcher.get_daily_data", lambda code, days=320: df)

    n = signal_tracker.evaluate_pending()
    assert n == 1
    conn = signal_tracker._db()
    row = conn.execute("SELECT ret_5d, ret_20d FROM signals").fetchone()
    conn.close()
    assert row[0] == pytest.approx(0.1)  # 100 -> 110
    assert row[1] is None  # 20 日数据不足


def test_weekly_summary(monkeypatch):
    signal_tracker.record_signals("600519", _result(buys=["MACD"]))
    dates = pd.date_range(pd.Timestamp.now().normalize(), periods=10, freq="B").strftime("%Y%m%d")
    df = pd.DataFrame({
        "code": "600519", "date": dates,
        "open": 100.0, "close": [100] * 3 + [110] * 7,
        "high": 101.0, "low": 99.0, "volume": 1,
    })
    monkeypatch.setattr("data_fetcher.get_daily_data", lambda code, days=320: df)
    signal_tracker.evaluate_pending()
    text = signal_tracker.weekly_summary()
    assert "MACD" in text
    assert "+10.00%" in text
    assert "胜率 100%" in text


def test_weekly_summary_empty():
    assert "无信号" in signal_tracker.weekly_summary()


def test_signal_older_than_week_excluded():
    """超过 N 周的信号不进战报。"""
    signal_tracker.record_signals("600519", _result(buys=["MACD"]))
    conn = signal_tracker._db()
    conn.execute("UPDATE signals SET ts=?", (int(time.time()) - 30 * 86400,))
    conn.commit()
    conn.close()
    assert "无信号" in signal_tracker.weekly_summary()
