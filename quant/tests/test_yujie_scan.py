"""yujie_scan 单元测试：合成数据 mock get_daily_data，不联网。"""

from unittest.mock import MagicMock

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


# ---------------- scan_all_cached 全市场扫描 ----------------


def test_scan_all_cached_with_limit(monkeypatch):
    """scan_all_cached 用 limit=10 应只扫 10 只,返回结构正确。"""
    import yujie_scan
    # mock sqlite 返回 10 只假股票
    fake_rows = [(f"60000{i}", 120, "20260820") for i in range(10)]

    def fake_connect(*args, **kwargs):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = fake_rows
        conn.close = lambda: None
        return conn

    # mock pd.read_sql 返回简单 df
    def fake_read_sql(query, conn, params=None):
        code = params[0] if params else ""
        n = 120
        import numpy as np
        import pandas as pd
        rng = np.random.default_rng(42)
        close = 10 + np.cumsum(rng.normal(0, 0.2, n))
        return pd.DataFrame({
            "code": [code] * n,
            "date": [f"20260{i:03d}" for i in range(n)],
            "open": close, "close": close,
            "high": close * 1.02, "low": close * 0.98,
            "volume": [1000000.0] * n,
        })

    monkeypatch.setattr("sqlite3.connect", fake_connect)
    monkeypatch.setattr("pandas.read_sql", fake_read_sql)
    result = yujie_scan.scan_all_cached(top_n=5, min_score=0, limit=10)
    assert "scanned" in result
    assert "hits_count" in result
    assert "hits" in result
    assert "elapsed_sec" in result
    assert result["scanned"] == 10
    assert isinstance(result["hits"], list)


def test_scan_all_cached_hits_structure(monkeypatch):
    """hits 中每条应有 code/score/hits/price 字段。"""
    import yujie_scan
    fake_rows = [("600519", 120, "20260820")]

    def fake_connect(*args, **kwargs):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = fake_rows
        conn.close = lambda: None
        return conn

    def fake_read_sql(query, conn, params=None):
        code = params[0] if params else "600519"
        import numpy as np
        import pandas as pd
        n = 120
        rng = np.random.default_rng(42)
        close = 10 + np.cumsum(rng.normal(0, 0.2, n))
        return pd.DataFrame({
            "code": [code] * n,
            "date": [f"20260{i:03d}" for i in range(n)],
            "open": close, "close": close,
            "high": close * 1.02, "low": close * 0.98,
            "volume": [1000000.0] * n,
        })

    monkeypatch.setattr("sqlite3.connect", fake_connect)
    monkeypatch.setattr("pandas.read_sql", fake_read_sql)
    result = yujie_scan.scan_all_cached(top_n=5, min_score=0, limit=1)
    for h in result["hits"]:
        assert "code" in h
        assert "score" in h
        assert "hits" in h
        assert "price" in h


def test_scan_all_cached_progress_callback(monkeypatch):
    """progress_callback 应被调用。"""
    import yujie_scan
    fake_rows = [(f"60000{i}", 120, "20260820") for i in range(10)]
    call_log = []

    def fake_connect(*args, **kwargs):
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = fake_rows
        conn.close = lambda: None
        return conn

    def fake_read_sql(query, conn, params=None):
        code = params[0] if params else ""
        import numpy as np
        import pandas as pd
        n = 120
        rng = np.random.default_rng(42)
        close = 10 + np.cumsum(rng.normal(0, 0.2, n))
        return pd.DataFrame({
            "code": [code] * n,
            "date": [f"20260{i:03d}" for i in range(n)],
            "open": close, "close": close,
            "high": close * 1.02, "low": close * 0.98,
            "volume": [1000000.0] * n,
        })

    monkeypatch.setattr("sqlite3.connect", fake_connect)
    monkeypatch.setattr("pandas.read_sql", fake_read_sql)

    def cb(scanned, total, hits_count):
        call_log.append((scanned, total, hits_count))

    yujie_scan.scan_all_cached(top_n=5, min_score=0, limit=10, progress_callback=cb)
    # 应至少被调用一次(完成时)
    assert len(call_log) >= 1
