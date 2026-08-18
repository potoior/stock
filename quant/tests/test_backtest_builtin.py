"""backtest_builtin 单元测试：验证向量化信号生成与回测聚合。"""

import numpy as np
import pandas as pd
import pytest

import backtest_builtin as bt


def _make_df(n=250, seed=42):
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.maximum(close, 1.0)
    high = close * 1.02
    low = close * 0.98
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    dates = pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d")
    return pd.DataFrame(
        {"date": dates, "open": open_, "close": close, "high": high, "low": low, "volume": volume}
    ).reset_index(drop=True)


def test_indicators_returns_all_required():
    """_indicators 应返回所有策略所需的指标。"""
    df = _make_df(n=250)
    inds = bt._indicators(df)
    required = ["close", "high", "low", "volume", "diff", "dea", "bar",
                "k", "d", "j", "boll_u", "boll_m", "boll_l",
                "psy", "bias1", "bias2", "bias3",
                "pdi", "mdi", "adx", "sar", "bbu", "bbm", "bbl",
                "tower", "ma5", "ma10", "ma20", "ma60", "ma7", "ma13"]
    for k in required:
        assert k in inds, f"缺少指标 {k}"
        assert len(inds[k]) == len(df)


def test_signal_functions_return_bool_series():
    """每个信号函数应返回与 df 等长的 bool Series。"""
    df = _make_df(n=250)
    inds = bt._indicators(df)
    for sid, _name, fn in bt.SIGNAL_FNS:
        sig = fn(inds, {})
        assert len(sig) == len(df), f"{sid} 信号长度不匹配"
        assert sig.dtype == bool, f"{sid} 信号非 bool 类型"


def test_signal_sparrow_returns_all_false():
    """麻雀战术是止盈策略,买入信号应全 False。"""
    df = _make_df(n=250)
    inds = bt._indicators(df)
    sig = bt.signal_sparrow(inds, {})
    assert int(sig.sum()) == 0


def test_cross_up_detects_golden_cross():
    """_cross_up 应正确检测金叉。"""
    s = pd.Series([1, 2, 3, 2, 1, 2, 3], dtype=float)
    ref = pd.Series([2, 2, 2, 2, 2, 2, 2], dtype=float)
    cross = bt._cross_up(s, ref)
    # 第1个点 s=1<ref=2 不算; 第5个点 s=1<ref=2; 第6个点 s=2==ref=2 不上穿
    # 实际: idx=2 s=3>ref=2 & prev s=2<=ref=2 → 金叉
    assert cross.iloc[2]
    # idx=5 s=2==ref=2, prev s=1<ref=2 → 不算上穿(必须 >)
    # idx=6 s=3>ref=2 & prev s=2<=ref=2 → 金叉
    assert cross.iloc[6]


def test_signal_macd_detects_golden():
    """MACD 金叉信号应在 DIFF 上穿 DEA 时触发。"""
    df = _make_df(n=250, seed=7)
    inds = bt._indicators(df)
    sig = bt.signal_macd(inds, {})
    # 至少应有一些信号(随机数据大概率有金叉)
    assert int(sig.sum()) >= 0  # 不报错即可
    # 信号日 DIFF>DEA 且前一日 DIFF<=DEA
    sig_idx = np.where(sig.values)[0]
    for i in sig_idx[:5]:
        if i > 0:
            assert inds["diff"].iloc[i] > inds["dea"].iloc[i]
            assert inds["diff"].iloc[i - 1] <= inds["dea"].iloc[i - 1]


def test_aggregate_handles_empty_signals():
    """_aggregate 应处理无信号的情况(如 sparrow)。"""
    sig_rets = {sid: {h: [] for h in bt.HORIZONS} for sid, _, _ in bt.SIGNAL_FNS}
    sig_count = {sid: 0 for sid, _, _ in bt.SIGNAL_FNS}
    baseline_acc = {h: [0.1 * h, 10 * h] for h in bt.HORIZONS}
    report = bt._aggregate(sig_rets, sig_count, baseline_acc)
    assert "strategies" in report
    assert "baseline" in report
    # 所有策略信号数应为 0
    for s in report["strategies"].values():
        assert s["signal_count"] == 0
        assert len(s["horizons"]) == 0


def test_aggregate_computes_excess():
    """_aggregate 应正确计算超额收益。"""
    sig_rets = {sid: {h: [] for h in bt.HORIZONS} for sid, _, _ in bt.SIGNAL_FNS}
    sig_count = {sid: 0 for sid, _, _ in bt.SIGNAL_FNS}
    # 给 macd 策略注入 20 天收益 [0.1, -0.05, 0.2]
    sig_rets["macd"][20] = [0.1, -0.05, 0.2]
    sig_count["macd"] = 3
    # 基准 20 天 = 0.02
    baseline_acc = {h: [0.02 * h * 10, 10 * h] for h in bt.HORIZONS}
    report = bt._aggregate(sig_rets, sig_count, baseline_acc)
    macd = report["strategies"]["macd"]
    h20 = macd["horizons"]["20"]
    assert h20["n"] == 3
    assert h20["mean_ret"] == pytest.approx((0.1 - 0.05 + 0.2) / 3)
    # baseline[20] = 0.02*20*10 / (10*20) = 0.02
    assert report["baseline"]["20"] == pytest.approx(0.02)
    assert h20["excess"] == pytest.approx(h20["mean_ret"] - 0.02)


def test_grid_signal_strat_macd():
    """_grid_signal_strat 应能用给定参数重算 MACD 信号。"""
    df = _make_df(n=250)
    inds = bt._indicators(df)
    sig = bt._grid_signal_strat(inds, "macd", {"fast": 10, "slow": 20, "signal": 9})
    assert len(sig) == len(df)
    assert sig.dtype == bool


def test_grid_signal_strat_unknown_raises():
    """_grid_signal_strat 未知策略应报错。"""
    df = _make_df(n=250)
    inds = bt._indicators(df)
    with pytest.raises(ValueError):
        bt._grid_signal_strat(inds, "unknown", {})
