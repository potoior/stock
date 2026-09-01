"""strategy_engine.analyze 主流程单元测试：mock 数据，不联网。"""

import numpy as np
import pandas as pd
import pytest

import strategy_engine as se


def _make_df(n=200, seed=42):
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.maximum(close, 1.0)
    high = close * 1.02
    low = close * 0.98
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"date": dates, "open": open_, "close": close, "high": high, "low": low, "volume": volume}
    ).reset_index(drop=True)


@pytest.fixture
def mock_data(monkeypatch):
    df = _make_df()
    monkeypatch.setattr("strategy_engine.get_daily_data", lambda code, days=320: df)
    monkeypatch.setattr(
        "strategy_engine.fetch_realtime",
        lambda codes: [{"code": "000001", "name": "测试股票", "price": 10.5, "pct": 1.2}],
    )
    return df


def test_analyze_returns_well_formed_result(mock_data):
    r = se.analyze("000001", use_ai=False)
    assert "realtime" in r
    assert "indicators" in r
    assert "indicator_series" in r
    assert "signals" in r
    assert "summary" in r
    assert "verdict" in r
    assert "kline" in r


def test_analyze_summary_counts(mock_data):
    r = se.analyze("000001", use_ai=False)
    s = r["summary"]
    assert s["buy"] + s["sell"] + s["hold"] == s["total"]
    assert s["total"] > 0


def test_analyze_signals_have_valid_action(mock_data):
    r = se.analyze("000001", use_ai=False)
    for s in r["signals"]:
        assert s["signal"] in ("buy", "sell", "hold")
        assert "name" in s
        assert "reason" in s
        assert "builtin" in s


def test_analyze_indicator_series_length(mock_data):
    r = se.analyze("000001", use_ai=False)
    s = r["indicator_series"]
    assert "dates" in s
    assert "macd_diff" in s
    assert "k" in s
    assert "boll_u" in s
    assert len(s["dates"]) == len(s["macd_diff"])
    assert len(s["dates"]) == len(s["k"])


def test_analyze_verdict_in_set(mock_data):
    r = se.analyze("000001", use_ai=False)
    assert r["verdict"] in ("买入", "卖出", "观望")
    assert r["verdict_icon"] in ("⬆", "⬇", "⏸")


def test_analyze_indicators_keys(mock_data):
    r = se.analyze("000001", use_ai=False)
    ind = r["indicators"]
    for key in ("macd_diff", "macd_dea", "macd_bar", "k", "d", "j",
                "boll_u", "boll_m", "boll_l", "ma5", "ma10", "ma20", "ma60",
                "psy", "bias1", "pdi", "mdi", "adx", "sar", "tower"):
        assert key in ind


def test_analyze_kline_shape(mock_data):
    r = se.analyze("000001", use_ai=False)
    kl = r["kline"]
    assert isinstance(kl, list)
    assert len(kl) <= 120
    if kl:
        first = kl[0]
        for key in ("date", "open", "close", "high", "low", "volume"):
            assert key in first


def test_analyze_insufficient_history(monkeypatch):
    short_df = _make_df(n=20)
    monkeypatch.setattr("strategy_engine.get_daily_data", lambda code, days=320: short_df)
    monkeypatch.setattr("strategy_engine.fetch_realtime", lambda codes: [])
    r = se.analyze("999999", use_ai=False)
    assert "error" in r


# ---------- verdict_from_votes 回归测试 ----------


def test_verdict_stalemate_is_hold():
    """买8/卖7/观38:多空对峙必须观望,不能判买入(2026-09 均胜电子案例)。"""
    verdict, _ = se.verdict_from_votes(8, 7, 53)
    assert verdict == "观望"


def test_verdict_40pct_threshold_buy():
    """达 40% 阈值直接定方向。"""
    verdict, _ = se.verdict_from_votes(21, 5, 53)
    assert verdict == "买入"
    verdict, _ = se.verdict_from_votes(5, 21, 53)
    assert verdict == "卖出"


def test_verdict_weak_fallback_needs_2x_margin():
    """弱回退:3+票且 2 倍优势才给方向。"""
    verdict, _ = se.verdict_from_votes(6, 3, 53)
    assert verdict == "买入"
    verdict, _ = se.verdict_from_votes(3, 6, 53)
    assert verdict == "卖出"
    # 一票之差不给方向
    verdict, _ = se.verdict_from_votes(4, 3, 53)
    assert verdict == "观望"


def test_verdict_all_hold():
    """全观望 → 观望。"""
    verdict, _ = se.verdict_from_votes(0, 0, 53)
    assert verdict == "观望"


def test_verdict_min_three_votes():
    """不足 3 票不给方向。"""
    verdict, _ = se.verdict_from_votes(2, 0, 5)
    assert verdict == "观望"
