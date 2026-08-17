"""策略指标计算的纯函数单元测试（使用合成数据，不联网）"""

import numpy as np
import pandas as pd

import strategy_engine as se


def make_df(n=200, seed=0):
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.normal(0, 0.2, n))
    close = np.maximum(close, 1.0)
    high = close * 1.02
    low = close * 0.98
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.integers(1_000_000, 5_000_000, n).astype(float)
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"open": open_, "close": close, "high": high, "low": low, "volume": volume}, index=idx
    )


def test_compute_macd_shapes():
    df = make_df()
    diff, dea, bar = se.compute_macd(df)
    assert len(diff) == len(df)
    assert len(dea) == len(df)
    assert len(bar) == len(df)
    assert np.allclose(bar, (diff - dea) * 2)


def test_compute_kdj_bounds():
    df = make_df()
    k, d, j, rsv = se.compute_kdj(df)
    valid = k.dropna()
    assert valid.between(0, 100).all()
    assert d.dropna().between(0, 100).all()


def test_compute_boll_ordering():
    df = make_df()
    upper, mid, lower = se.compute_boll(df)
    v = pd.DataFrame({"u": upper, "m": mid, "l": lower}).dropna()
    assert (v["u"] >= v["m"]).all()
    assert (v["m"] >= v["l"]).all()


def test_compute_bias():
    df = make_df()
    b1, b2, b3 = se.compute_bias(df)
    assert len(b1) == len(df)
    assert b1.dropna().abs().max() < 200


def test_compute_tower_values():
    df = make_df()
    tw = se.compute_tower(df)
    vals = set(tw.dropna().unique())
    assert vals.issubset({-1, 0, 1})


def test_compute_bbiboll_ordering():
    df = make_df()
    upper, mid, lower = se.compute_bbiboll(df)
    v = pd.DataFrame({"u": upper, "m": mid, "l": lower}).dropna()
    assert (v["u"] >= v["m"]).all()
    assert (v["m"] >= v["l"]).all()
