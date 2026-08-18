"""书2新增策略(RSI/抄底/逃顶/涨停板)单元测试。"""

import numpy as np
import pandas as pd

import strategy_engine as se


def _make_df(n=250, seed=42, trend=0.0):
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


def _ctx(df, i=None):
    if i is None:
        i = len(df) - 1
    return {
        "i": i,
        "price": float(df["close"].iloc[i]),
        "df": df,
        "close": df["close"],
    }


# ---------------- RSI ----------------


def test_rsi_oversold_buy():
    """构造连续下跌让RSI跌破超卖阈值,应发买入信号。"""
    n = 60
    close = pd.Series(np.linspace(20, 5, n))  # 持续下跌
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    sig, reason = se.strategy_rsi(_ctx(df), {"p1": 6, "p2": 12, "oversold": 30, "overbought": 70})
    assert sig == "buy"
    assert "超卖" in reason


def test_rsi_overbought_sell():
    """构造连续上涨让RSI冲上超买阈值,应发卖出信号。"""
    n = 60
    # 指数式上涨,确保每日都涨,RSI→100
    close = pd.Series(5 * np.power(1.03, np.arange(n)))
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    sig, reason = se.strategy_rsi(_ctx(df), {"p1": 6, "p2": 12, "oversold": 30, "overbought": 70})
    assert sig == "sell"
    assert "超买" in reason or "死叉" in reason or "空头" in reason


def test_rsi_normal_data_returns_signal():
    """普通数据应返回 buy/sell/hold 之一,不报错。"""
    df = _make_df(n=250, seed=42)
    sig, _ = se.strategy_rsi(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["rsi"])
    assert sig in ("buy", "sell", "hold")


# ---------------- 抄底 ----------------


def test_bottom_data_insufficient():
    """数据不足应返回 hold。"""
    df = _make_df(n=10, seed=1)
    sig, reason = se.strategy_bottom(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["bottom"])
    assert sig == "hold"
    assert "数据不足" in reason


def test_bottom_drop_with_shrink_buy():
    """构造大跌+缩量,应触发抄底买入。"""
    n = 50
    close = np.concatenate([
        np.full(30, 20.0),  # 前30日平稳
        np.linspace(20, 10, 20),  # 后20日大跌
    ])
    volume = np.concatenate([
        np.full(30, 5e6),
        np.full(19, 5e6),
        [1e6],  # 末日极度缩量
    ])
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    params = {"lookback": 20, "vol_shrink": 0.5, "drop_pct": -5}
    sig, reason = se.strategy_bottom(_ctx(df), params)
    assert sig == "buy"
    assert "缩量" in reason or "底背离" in reason


def test_bottom_no_signal_normal():
    """正常行情应无抄底信号。"""
    df = _make_df(n=250, seed=42)
    sig, _ = se.strategy_bottom(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["bottom"])
    # 随机数据可能触发也可能不触发,只验证不报错且返回合法值
    assert sig in ("buy", "hold")


# ---------------- 逃顶 ----------------


def test_top_data_insufficient():
    df = _make_df(n=10, seed=1)
    sig, reason = se.strategy_top(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["top"])
    assert sig == "hold"
    assert "数据不足" in reason


def test_top_hot_volume_sell():
    """构造大涨+天量,应触发逃顶卖出。"""
    n = 50
    close = np.concatenate([
        np.full(30, 5.0),
        np.linspace(5, 15, 20),  # 大涨
    ])
    volume = np.concatenate([
        np.full(30, 5e6),
        np.full(19, 5e6),
        [20e6],  # 末日天量
    ])
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    params = {"lookback": 20, "vol_expand": 2.0, "rise_pct": 5}
    sig, reason = se.strategy_top(_ctx(df), params)
    assert sig == "sell"
    assert "天量" in reason


def test_top_no_signal_normal():
    df = _make_df(n=250, seed=42)
    sig, _ = se.strategy_top(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["top"])
    assert sig in ("sell", "hold")


# ---------------- 涨停板 ----------------


def test_zt_limit_up_buy():
    """构造涨停(涨幅>=9.8%)+放量,应发买入信号。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.0  # 末日涨停 10%
    volume = np.full(n, 1e6)
    volume[-1] = 3e6  # 末日放量
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    params = {"zt_pct": 9.6, "min_vol_ratio": 1.5}
    sig, reason = se.strategy_zt(_ctx(df), params)
    assert sig == "buy"
    assert "涨停" in reason


def test_zt_no_signal_normal():
    """普通行情无涨停。"""
    df = _make_df(n=250, seed=42)
    sig, _ = se.strategy_zt(_ctx(df), se.DEFAULT_STRATEGY_PARAMS["zt"])
    assert sig in ("hold",)


def test_zt_low_volume_hold():
    """涨停但量比不足应观望(可能一字板)。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.0  # 涨停
    volume = np.full(n, 1e6)  # 无放量
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    sig, reason = se.strategy_zt(_ctx(df), {"zt_pct": 9.6, "min_vol_ratio": 1.5})
    assert sig == "hold"
    assert "量比" in reason or "不足" in reason


# ---------------- 注册到 BUILTIN ----------------


def test_new_strategies_in_builtin_list():
    """4个新策略应已在 BUILTIN 列表注册(通过 analyze 返回信号验证)。"""
    import re
    src = open(se.__file__).read()
    for sid in ("rsi", "bottom", "top", "zt"):
        # 在 BUILTIN 列表中
        pattern = rf'\("{sid}",\s*"[^"]+",\s*strategy_{sid}\)'
        assert re.search(pattern, src), f"{sid} 未注册到 BUILTIN"


def test_default_params_includes_new():
    """DEFAULT_STRATEGY_PARAMS 应包含4个新策略。"""
    for sid in ("rsi", "bottom", "top", "zt"):
        assert sid in se.DEFAULT_STRATEGY_PARAMS, f"{sid} 不在 DEFAULT_STRATEGY_PARAMS"
