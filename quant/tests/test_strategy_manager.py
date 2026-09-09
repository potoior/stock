"""策略总管测试:注册表元数据 + 预设组合 + 按需选择分析。"""

import pytest

import market_scan as ms
import strategy_engine as se


def test_strategy_registry_metadata():
    """每个内置策略都应有分类元数据。"""
    registry_ids = {sid for sid, _, _ in se.BUILTIN_REGISTRY if isinstance(sid, str)}
    meta_ids = set(se.STRATEGY_CATEGORY.keys())
    assert registry_ids == meta_ids, f"注册表与分类表不一致: {registry_ids ^ meta_ids}"


def test_presets_all_valid():
    """预设组合引用的策略 id 必须全部存在。"""
    for name, preset in se.STRATEGY_PRESETS.items():
        assert preset["ids"], f"预设 {name} 为空"
        for sid in preset["ids"]:
            assert sid in ms.BUILTIN_STRATEGY_IDS, f"预设 {name} 引用未知策略 {sid}"


def test_expand_strategy_ids_mixed():
    ids, unknown = se.expand_strategy_ids(["短线", "macd"])
    assert set(se.STRATEGY_PRESETS["短线"]["ids"]) <= set(ids)
    assert "macd" in ids


def test_expand_strategy_ids_unknown():
    ids, unknown = se.expand_strategy_ids(["短线", "no_such"])
    assert "no_such" in unknown
    assert "macd" in ids  # 短线预设展开正常


def test_expand_strategy_ids_empty():
    ids, unknown = se.expand_strategy_ids([])
    assert ids == []
    assert unknown == []


def test_analyze_with_strategies_unknown(monkeypatch):
    res = se.analyze_with_strategies("600519", ["nope"])
    assert "error" in res


def test_analyze_with_strategies_empty(monkeypatch):
    res = se.analyze_with_strategies("600519", [])
    assert "error" in res


@pytest.fixture
def mock_market(monkeypatch):
    """mock 掉联网,返回可预期的行情数据(温和上涨,MA5 上方)。"""
    import pandas as pd

    n = 200
    close = [10.0 + i * 0.05 for i in range(n)]  # 单边缓涨
    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="B"),
            "open": [c - 0.05 for c in close],
            "close": close,
            "high": [c + 0.1 for c in close],
            "low": [c - 0.1 for c in close],
            "volume": [100.0] * n,
        }
    )
    monkeypatch.setattr(se, "get_daily_data", lambda code, days=320: df)
    monkeypatch.setattr(se, "fetch_realtime", lambda codes: [])
    yield


def test_analyze_subset_runs_only_selected(mock_market, monkeypatch):
    """按需模式:只跑指定策略,且不受全局 enabled 开关影响。"""
    monkeypatch.setattr(se, "get_strategies", lambda: [])
    res = se.analyze("600519", use_ai=False, strategy_ids=["macd", "kdj"])
    assert "error" not in res
    keys = {s["key"] for s in res["signals"]}
    assert keys == {"macd", "kdj"}


def test_analyze_subset_overrides_disabled(mock_market, monkeypatch):
    """按需模式:显式选择的优先于全局 enabled 开关。"""
    monkeypatch.setattr(
        se,
        "get_strategies",
        lambda: [{"id": "macd", "type": "builtin", "enabled": False, "params": {}}],
    )
    res = se.analyze("600519", use_ai=False, strategy_ids=["macd"])
    keys = {s["key"] for s in res["signals"]}
    assert keys == {"macd"}


def test_analyze_all_respects_enabled(mock_market, monkeypatch):
    """全量模式(不传 strategy_ids):disabled 的策略不跑。"""
    monkeypatch.setattr(
        se,
        "get_strategies",
        lambda: [{"id": "macd", "type": "builtin", "enabled": False, "params": {}}],
    )
    res = se.analyze("600519", use_ai=False)
    keys = {s["key"] for s in res["signals"]}
    assert "macd" not in keys


def test_analyze_with_strategies_preset(mock_market, monkeypatch):
    monkeypatch.setattr(se, "get_strategies", lambda: [])
    res = se.analyze_with_strategies("600519", ["抄底"], use_ai=False)
    assert "error" not in res
    keys = {s["key"] for s in res["signals"]}
    assert keys == set(se.STRATEGY_PRESETS["抄底"]["ids"])


# ============ set_strategy_params 白名单校验 ============


def test_validate_params_ok():
    assert se.validate_strategy_params("macd", {"fast": 10}) is None


def test_validate_params_unknown_name():
    res = se.validate_strategy_params("macd", {"not_a_param": 1})
    assert res and "未知参数" in res


def test_validate_params_non_numeric():
    res = se.validate_strategy_params("macd", {"fast": "abc"})
    assert res and "必须是数字" in res


def test_validate_params_out_of_range():
    res = se.validate_strategy_params("macd", {"fast": 999999})
    assert res and "范围" in res


def test_validate_params_no_param_strategy():
    """无参数策略(tower 等)传任何参数都拒绝。"""
    res = se.validate_strategy_params("tower", {"n": 5})
    assert res and "无可调参数" in res


def test_validate_params_unknown_strategy():
    res = se.validate_strategy_params("no_such_strategy", {"x": 1})
    assert res and "未知策略" in res


def test_validate_params_bool_rejected():
    res = se.validate_strategy_params("macd", {"fast": True})
    assert res and "必须是数字" in res
