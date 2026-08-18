"""yujie_scan 单元测试：合成数据 mock get_daily_data，不联网。"""

import numpy as np
import pandas as pd
import pytest

import yujie_scan


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
    monkeypatch.setattr("yujie_scan.get_daily_data", lambda code, days=320: df)
    return df


def test_score_stock_returns_tuple(mock_data):
    sc, hits, detail = yujie_scan.score_stock("000001", yujie_scan.DEFAULT_PARAMS)
    assert isinstance(sc, float)
    assert isinstance(hits, list)
    assert detail is None or isinstance(detail, dict)


def test_score_stock_min_history_filter(monkeypatch):
    df = _make_df(n=30)
    monkeypatch.setattr("yujie_scan.get_daily_data", lambda code, days=320: df)
    sc, hits, detail = yujie_scan.score_stock("000001", yujie_scan.DEFAULT_PARAMS)
    assert sc == 0
    assert hits == []
    assert detail is None


def test_score_stock_score_in_expected_range(mock_data):
    params = yujie_scan.DEFAULT_PARAMS
    sc, hits, _ = yujie_scan.score_stock("000001", params)
    # 满分 11 分(默认参数下所有规则都命中)
    assert 0 <= sc <= 11.5


def test_get_params_returns_defaults(monkeypatch, tmp_path):
    # CONFIG_PATH 不存在时回退到 DEFAULT_PARAMS
    monkeypatch.setattr("yujie_scan.CONFIG_PATH", tmp_path / "no_config.json")
    p = yujie_scan.get_params()
    assert p == yujie_scan.DEFAULT_PARAMS


def test_get_params_merges_saved(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"yujie": {"scope": {"min_amount_yi": 1.5}}}', encoding="utf-8")
    monkeypatch.setattr("yujie_scan.CONFIG_PATH", cfg_path)
    p = yujie_scan.get_params()
    # 覆盖生效
    assert p["scope"]["min_amount_yi"] == 1.5
    # 其他默认保留
    assert p["scope"]["min_history_days"] == yujie_scan.DEFAULT_PARAMS["scope"]["min_history_days"]
    assert p["macd"]["golden_score"] == yujie_scan.DEFAULT_PARAMS["macd"]["golden_score"]


def test_save_params_roundtrip(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("yujie_scan.CONFIG_PATH", cfg_path)
    new_params = yujie_scan.get_params()
    new_params["macd"]["golden_score"] = 5
    yujie_scan.save_params(new_params)
    loaded = yujie_scan.get_params()
    assert loaded["macd"]["golden_score"] == 5


def test_hit_label_mapping():
    assert yujie_scan._hit_label("macd_golden") == "MACD金叉"
    assert yujie_scan._hit_label("mos_bottom") == "MOS低点"
    # 未知 rule_id 返回原值
    assert yujie_scan._hit_label("unknown_rule") == "unknown_rule"


def test_get_rank_missing_code(monkeypatch, tmp_path):
    # 用临时 db,空表
    monkeypatch.setattr("yujie_scan.CACHE_DB", tmp_path / "test.db")
    assert yujie_scan.get_rank("999999", "20260101") is None


def test_get_rank_after_save(monkeypatch, tmp_path):
    monkeypatch.setattr("yujie_scan.CACHE_DB", tmp_path / "test.db")
    picks = [
        {"code": "600519", "name": "贵州茅台", "score": 8.0, "hits": ["MACD金叉"], "detail": {"price": 1500}},
        {"code": "000001", "name": "平安银行", "score": 5.0, "hits": [], "detail": {}},
    ]
    yujie_scan.save_picks("20260101", picks)
    assert yujie_scan.get_rank("600519", "20260101") == 1
    assert yujie_scan.get_rank("000001", "20260101") == 2
    assert yujie_scan.get_rank("999999", "20260101") is None


def test_load_picks_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr("yujie_scan.CACHE_DB", tmp_path / "test.db")
    picks = [
        {"code": "600519", "name": "贵州茅台", "score": 8.0, "hits": ["MACD金叉"], "detail": {"price": 1500}},
    ]
    yujie_scan.save_picks("20260101", picks)
    loaded = yujie_scan.load_picks("20260101")
    assert len(loaded) == 1
    assert loaded[0]["code"] == "600519"
    assert loaded[0]["rank"] == 1
    assert loaded[0]["hits"] == ["MACD金叉"]
    assert loaded[0]["detail"]["price"] == 1500
