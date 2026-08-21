"""strategy_engine.scan_with_strategy 单元测试(全市场策略选股)。

不实际跑全市场(耗时),用 limit=10 + mock get_daily_data 验证逻辑。
"""

from unittest.mock import patch

import numpy as np
import pandas as pd

import strategy_engine as se


def _make_df(n=120, seed=42, trend=0.0):
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(trend, 0.2, n))
    close = np.maximum(close, 1.0)
    high = close * 1.02
    low = close * 0.98
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    dates = pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d")
    return pd.DataFrame(
        {"date": dates, "open": open_, "close": close, "high": high, "low": low, "volume": volume}
    ).reset_index(drop=True)


def test_scan_invalid_strategy_returns_error():
    """未知策略 id 应返回 error。"""
    result = se.scan_with_strategy("nonexistent_strategy")
    assert "error" in result
    assert "未知策略" in result["error"]


def test_scan_returns_hits_structure():
    """正常扫描应返回 scanned/hits_count/hits/elapsed_sec 结构。"""
    df = _make_df(n=120, seed=42)
    # mock get_daily_data 避免联网 + 加速
    with patch("strategy_engine.get_daily_data", return_value=df):
        # mock daily 表查询返回 3 只假股票
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
                ("000001", 120, "20260820"),
                ("300750", 120, "20260820"),
            ]
            result = se.scan_with_strategy("macd", top_n=10, min_amount_yi=0)
    assert "error" not in result
    assert "scanned" in result
    assert "hits_count" in result
    assert "hits" in result
    assert "elapsed_sec" in result
    assert isinstance(result["hits"], list)
    assert result["scanned"] == 3


def test_scan_filters_by_min_amount_yi():
    """成交额 < min_amount_yi 的股票应被过滤。"""
    # 构造小成交额数据(volume=1e6, price~10 → amount_yi ~0.001 亿)
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
            ]
            # min_amount_yi=10 应过滤掉所有(volume*price ~ 1e7 = 0.01 亿)
            result = se.scan_with_strategy("macd", top_n=10, min_amount_yi=10)
    assert result["hits_count"] == 0
    assert result["hits"] == []


def test_scan_only_buy_signals():
    """hits 中只应包含 signal='buy' 的股票。"""
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
                ("000001", 120, "20260820"),
            ]
            result = se.scan_with_strategy("macd", top_n=10, min_amount_yi=0)
    for h in result["hits"]:
        assert h["signal"] == "buy"


def test_scan_hit_fields():
    """每条 hit 应有 code/price/pct/signal/reason/amount_yi 字段。"""
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
            ]
            result = se.scan_with_strategy("macd", top_n=10, min_amount_yi=0)
    for h in result["hits"]:
        assert "code" in h
        assert "price" in h
        assert "pct" in h
        assert "signal" in h
        assert "reason" in h
        assert "amount_yi" in h


def test_scan_top_n_limit():
    """top_n 应限制返回条数。"""
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
                ("000001", 120, "20260820"),
                ("300750", 120, "20260820"),
            ]
            result = se.scan_with_strategy("macd", top_n=1, min_amount_yi=0)
    assert result["hits_count"] <= 1


def test_scan_limit_param_restricts_universe():
    """limit 参数应限制扫描股票数。"""
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
                ("000001", 120, "20260820"),
                ("300750", 120, "20260820"),
                ("301189", 120, "20260820"),
                ("600036", 120, "20260820"),
            ]
            result = se.scan_with_strategy("macd", top_n=10, min_amount_yi=0, limit=2)
    assert result["scanned"] == 2  # 只扫了 2 只
