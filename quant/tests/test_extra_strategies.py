"""22 个新策略(操练大全12/14/15/16/17/20章 + 漫画书 + 基本面)单元测试。

覆盖:
  - 12章: trend_follow / pyramid / stop_profit / plan_trade
  - 14章: pe_select / roe_pe
  - 15章: bottom_ma
  - 16章: top_weekly / top_monthly
  - 17章: zhuang_test / build / pull / ship / wash
  - 20章: zt_type / zt_unsealed / zt_pull
  - 漫画书: high_volume / demon_stock / dragon_pullback / support_resistance / range_trade
  - 注册到 BUILTIN 列表 + DEFAULT_STRATEGY_PARAMS
"""

import numpy as np
import pandas as pd

import strategy_engine as se

NEW_STRATEGY_IDS = [
    "trend_follow", "pyramid", "stop_profit", "plan_trade",
    "high_volume", "demon_stock", "dragon_pullback",
    "support_resistance", "range_trade",
    "bottom_ma", "top_weekly", "top_monthly",
    "zhuang_test", "zhuang_build", "zhuang_pull", "zhuang_ship", "zhuang_wash",
    "zt_type", "zt_unsealed", "zt_pull",
    "pe_select", "roe_pe",
]


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


def _ctx(df, i=None, code="600519"):
    if i is None:
        i = len(df) - 1
    close = df["close"]
    macd_diff, macd_dea, _ = se.compute_macd(df)
    return {
        "i": i,
        "price": float(close.iloc[i]),
        "df": df,
        "close": close,
        "code": code,
        "ma5": close.rolling(5).mean(),
        "ma10": close.rolling(10).mean(),
        "ma20": close.rolling(20).mean(),
        "ma60": close.rolling(60).mean(),
        "adx": pd.Series(np.full(len(df), 25.0)),
        "macd_diff": macd_diff,
        "macd_dea": macd_dea,
    }


# ---------------- 注册 + 默认参数 ----------------


def test_all_new_strategies_in_default_params():
    """22 个新策略都应在 DEFAULT_STRATEGY_PARAMS 中。"""
    for sid in NEW_STRATEGY_IDS:
        assert sid in se.DEFAULT_STRATEGY_PARAMS, f"{sid} 不在 DEFAULT_STRATEGY_PARAMS"


def test_all_new_strategies_in_builtin_list():
    """22 个新策略都应在 analyze() 的 BUILTIN 列表中。"""
    import re
    src = open(se.__file__).read()
    for sid in NEW_STRATEGY_IDS:
        pattern = rf'\("{sid}",\s*"[^"]+",\s*strategy_{sid}\)'
        assert re.search(pattern, src), f"{sid} 未注册到 BUILTIN"


def test_all_new_strategies_callable_with_default_params():
    """22 个新策略在普通数据上不报错,返回 buy/sell/hold 之一。"""
    df = _make_df(n=250, seed=42)
    ctx = _ctx(df)
    for sid in NEW_STRATEGY_IDS:
        fn = getattr(se, f"strategy_{sid}")
        sg, rsn = fn(ctx, se.DEFAULT_STRATEGY_PARAMS[sid])
        assert sg in ("buy", "sell", "hold"), f"{sid} 返回非法信号: {sg}"
        assert isinstance(rsn, str) and rsn, f"{sid} reason 为空"


def test_data_insufficient_returns_hold(monkeypatch):
    """数据不足应返回 hold。pe_select/roe_pe 走外部财务数据,mock 返回 None。"""
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: None)
    df = _make_df(n=5, seed=1)
    ctx = _ctx(df)
    for sid in NEW_STRATEGY_IDS:
        fn = getattr(se, f"strategy_{sid}")
        sg, _ = fn(ctx, se.DEFAULT_STRATEGY_PARAMS[sid])
        assert sg == "hold", f"{sid} 数据不足时应 hold,实际 {sg}"


# ---------------- 12章 投资法则 ----------------


def test_trend_follow_strong_bull_buy():
    """ADX 强趋势 + 均线多头排列 → buy。"""
    n = 100
    close = pd.Series(np.linspace(5, 20, n))  # 持续上涨 → 多头排列
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    ctx["adx"] = pd.Series(np.full(n, 30.0))  # ADX=30 > 25 强趋势
    sg, reason = se.strategy_trend_follow(ctx, se.DEFAULT_STRATEGY_PARAMS["trend_follow"])
    assert sg == "buy"
    assert "多头" in reason


def test_trend_follow_strong_bear_sell():
    """ADX 强趋势 + 均线空头排列 → sell。"""
    n = 100
    close = pd.Series(np.linspace(20, 5, n))  # 持续下跌 → 空头排列
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    ctx["adx"] = pd.Series(np.full(n, 30.0))
    sg, reason = se.strategy_trend_follow(ctx, se.DEFAULT_STRATEGY_PARAMS["trend_follow"])
    assert sg == "sell"
    assert "空头" in reason


def test_trend_follow_weak_trend_hold():
    """ADX 弱(<20)→ hold。"""
    n = 100
    df = _make_df(n=n, seed=42)
    ctx = _ctx(df)
    ctx["adx"] = pd.Series(np.full(n, 15.0))  # ADX=15 < 20 无趋势
    sg, reason = se.strategy_trend_follow(ctx, se.DEFAULT_STRATEGY_PARAMS["trend_follow"])
    assert sg == "hold"
    assert "无" in reason or "不明" in reason


def test_pyramid_below_base_buy():
    """价格低于基准 × (1-step) → buy。"""
    n = 30
    close = np.concatenate([np.full(25, 10.0), np.linspace(10, 8, 5)])  # 末日跌到 8
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_pyramid(ctx, {"n": 20, "step": 0.1})
    assert sg == "buy"
    assert "加仓" in reason


def test_pyramid_above_base_sell():
    """价格高于基准 × (1+step) → sell。"""
    n = 30
    close = np.concatenate([np.full(25, 10.0), np.linspace(10, 12, 5)])
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_pyramid(ctx, {"n": 20, "step": 0.1})
    assert sg == "sell"
    assert "减仓" in reason


def test_stop_profit_short_term_surge_sell():
    """近 5 日累计涨幅 ≥ 20% → sell。"""
    n = 30
    close = np.concatenate([np.full(25, 10.0), np.linspace(10, 13, 5)])  # 5 日涨 30%
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_stop_profit(ctx, {"short": 5, "long": 10, "short_pct": 20, "long_pct": 30})
    assert sg == "sell"
    assert "暴利" in reason or "止盈" in reason


def test_plan_trade_death_cross_sell():
    """MACD 死叉 → sell。"""
    n = 60
    df = _make_df(n=n, seed=42)
    ctx = _ctx(df)
    # 构造死叉在 i=59 处:前 59 日 diff>dea,末日 diff<dea
    diff = pd.Series(np.concatenate([np.full(59, 0.5), [-0.5]]))
    dea = pd.Series(np.concatenate([np.full(59, 0.3), [-0.3]]))
    ctx["macd_diff"] = diff
    ctx["macd_dea"] = dea
    sg, reason = se.strategy_plan_trade(ctx, se.DEFAULT_STRATEGY_PARAMS["plan_trade"])
    assert sg == "sell"
    assert "死叉" in reason or "止损" in reason


def test_plan_trade_price_below_ma10_sell():
    """价格跌破 MA10 → sell。"""
    n = 60
    close = np.concatenate([np.full(40, 10.0), np.linspace(10, 7, 20)])  # 末日跌到 7
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    diff, dea, _ = se.compute_macd(df)
    ctx["macd_diff"] = diff
    ctx["macd_dea"] = dea
    sg, reason = se.strategy_plan_trade(ctx, se.DEFAULT_STRATEGY_PARAMS["plan_trade"])
    assert sg == "sell"
    assert "跌破" in reason or "死叉" in reason


# ---------------- 漫画书 量能/实战战法 ----------------


def test_high_volume_breakout_buy():
    """放量突破前高 → buy。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 12.0  # 突破前高
    volume = np.full(n, 1e6)
    volume[-1] = 5e6  # 高量柱
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_high_volume(ctx, {"n": 20})
    assert sg == "buy"
    assert "高量柱" in reason or "突破" in reason


def test_demon_stock_overheated_sell():
    """近 5 日累计涨幅 ≥ 30% → sell(过热)。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 13.5  # 5 日累计涨 35%
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_demon_stock(ctx, {"consec": 3, "consec_pct": 5, "hot": 5, "hot_pct": 30})
    assert sg == "sell"
    assert "过热" in reason


def test_dragon_pullback_no_zt_hold():
    """近 30 日无涨停 → hold。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    sg, reason = se.strategy_dragon_pullback(ctx, se.DEFAULT_STRATEGY_PARAMS["dragon_pullback"])
    assert sg == "hold"
    assert "无涨停" in reason


def test_support_resistance_breakout_buy():
    """放量突破 N 日高点 → buy。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 12.0  # 突破 N 日高
    volume = np.full(n, 1e6)
    volume[-1] = 3e6  # 放量
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_support_resistance(ctx, {"n": 20, "vol_ratio": 1.5})
    assert sg == "buy"
    assert "突破" in reason


def test_range_trade_near_support_buy():
    """价格触及支撑位 → buy。"""
    n = 30
    close = np.concatenate([np.full(28, 10.0), [9.0, 9.05]])  # 末日触及支撑
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_range_trade(ctx, {"n": 20, "low_pct": 0.2, "high_pct": 0.2})
    assert sg == "buy"
    assert "支撑" in reason or "低买" in reason


def test_range_trade_near_resistance_sell():
    """价格触及压力位 → sell。"""
    n = 30
    close = np.concatenate([np.full(28, 10.0), [11.0, 11.5]])  # 末日触及压力
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_range_trade(ctx, {"n": 20, "low_pct": 0.2, "high_pct": 0.2})
    assert sg == "sell"
    assert "压力" in reason or "高卖" in reason


# ---------------- 15章 抄底(均线) ----------------


def test_bottom_ma_no_signal_normal():
    """普通数据无均线确认底信号。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    sg, _ = se.strategy_bottom_ma(ctx, se.DEFAULT_STRATEGY_PARAMS["bottom_ma"])
    assert sg in ("buy", "hold")


# ---------------- 16章 逃顶(周/月线) ----------------


def test_top_weekly_big_rise_with_shadow_sell():
    """周巨阳 + 长上影 → sell。"""
    n = 30
    close = np.concatenate([np.full(29, 10.0), [12.5]])
    high = np.concatenate([np.full(29, 10.2), [15.0]])  # 末日高 15 → 上影 2.5
    low = np.concatenate([np.full(29, 9.8), [10.4]])
    open_ = np.concatenate([np.full(29, 10.0), [10.5]])
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": open_, "close": close, "high": high, "low": low,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_top_weekly(ctx, {"rise_pct": 15})
    # 近 5 日:开 10,收 12.5,涨 25% > 15%;上影 2.5 > 实体 2.0
    assert sg == "sell"
    assert "周" in reason


def test_top_monthly_big_rise_sell():
    """月巨阳(近 22 日涨 ≥ 25%)→ sell。"""
    n = 30
    close = np.concatenate([np.full(8, 10.0), np.linspace(10, 13, 22)])
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_top_monthly(ctx, {"rise_pct": 25})
    assert sg == "sell"
    assert "月" in reason


# ---------------- 17章 跟庄 ----------------


def test_zhuang_test_long_shadow_low_pos_hold():
    """长上影 + 缩量 + 低位 → hold(试盘提示)。"""
    n = 100
    close = np.concatenate([np.linspace(20, 5, 80), np.full(20, 5.0)])  # 低位
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [5e6] * n,
    })
    df.loc[n - 1, "open"] = 5.0
    df.loc[n - 1, "close"] = 5.1
    df.loc[n - 1, "high"] = 6.0  # 上影 0.9 > 实体 0.1 × 2
    df.loc[n - 1, "low"] = 4.95
    df.loc[n - 1, "volume"] = 1e6  # 缩量
    ctx = _ctx(df)
    sg, reason = se.strategy_zhuang_test(ctx, se.DEFAULT_STRATEGY_PARAMS["zhuang_test"])
    assert sg == "hold"
    assert "试盘" in reason


def test_zhuang_pull_vol_breakout_buy():
    """放量 + 大阳 + 突破 → buy(拉高)。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.5  # 涨 15% + 突破
    volume = np.full(n, 1e6)
    volume[-1] = 5e6  # 量比 5
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    ctx = _ctx(df)
    sg, reason = se.strategy_zhuang_pull(ctx, se.DEFAULT_STRATEGY_PARAMS["zhuang_pull"])
    assert sg == "buy"
    assert "拉高" in reason or "拉升" in reason


def test_zhuang_ship_high_vol_stale_sell():
    """高位 + 放量 + 滞涨 → sell(出货)。"""
    n = 100
    close = np.concatenate([np.linspace(5, 20, 80), np.full(20, 20.0)])  # 高位横盘
    volume = np.full(n, 1e6)
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    df.loc[n - 5:n, "volume"] = 3e6  # 5/20 量比 3
    df.loc[n - 5:n, "close"] = [20.0, 20.1, 20.0, 20.1, 20.0]  # 5 日累计涨 0%
    ctx = _ctx(df)
    sg, reason = se.strategy_zhuang_ship(ctx, se.DEFAULT_STRATEGY_PARAMS["zhuang_ship"])
    assert sg == "sell"
    assert "出货" in reason


def test_zhuang_wash_no_signal_normal():
    """普通数据无洗盘特征。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    sg, _ = se.strategy_zhuang_wash(ctx, se.DEFAULT_STRATEGY_PARAMS["zhuang_wash"])
    assert sg == "hold"


# ---------------- 20章 涨停细分 ----------------


def test_zt_type_one_word_buy():
    """一字板 → buy。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.0  # 涨停 10%
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": [1e6] * n,
    })
    df.loc[n - 1, "open"] = 11.0
    df.loc[n - 1, "high"] = 11.0
    df.loc[n - 1, "low"] = 11.0
    df.loc[n - 1, "close"] = 11.0
    ctx = _ctx(df)
    sg, reason = se.strategy_zt_type(ctx, se.DEFAULT_STRATEGY_PARAMS["zt_type"])
    assert sg == "buy"
    assert "一字板" in reason


def test_zt_type_not_zt_hold():
    """未涨停 → hold。"""
    df = _make_df(n=250, seed=42)
    ctx = _ctx(df)
    sg, _ = se.strategy_zt_type(ctx, se.DEFAULT_STRATEGY_PARAMS["zt_type"])
    assert sg == "hold"


def test_zt_unsealed_open_board_sell():
    """涨停但盘中开板 + 放量 → sell。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 11.0  # 涨停 10%
    volume = np.full(n, 1e6)
    volume[-1] = 5e6  # 放量
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    df.loc[n - 1, "low"] = 10.5  # 开板(low 远低于 close 11)
    df.loc[n - 1, "high"] = 11.5
    df.loc[n - 1, "open"] = 10.5
    ctx = _ctx(df)
    sg, reason = se.strategy_zt_unsealed(ctx, se.DEFAULT_STRATEGY_PARAMS["zt_unsealed"])
    assert sg == "sell"
    assert "封不牢" in reason or "开板" in reason


def test_zt_pull_near_zt_with_vol_hold():
    """接近涨停 + 放量 + 大阳 → hold(观望)。"""
    n = 30
    close = np.full(n, 10.0)
    close[-1] = 10.7  # 涨 7%(在 5~9.6 区间)
    volume = np.full(n, 1e6)
    volume[-1] = 3e6  # 量比 3
    df = pd.DataFrame({
        "date": pd.date_range("2023-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": close, "close": close, "high": close * 1.01, "low": close * 0.99,
        "volume": volume,
    })
    df.loc[n - 1, "open"] = 10.0
    df.loc[n - 1, "close"] = 10.7
    df.loc[n - 1, "high"] = 10.8
    df.loc[n - 1, "low"] = 10.0
    ctx = _ctx(df)
    sg, reason = se.strategy_zt_pull(ctx, se.DEFAULT_STRATEGY_PARAMS["zt_pull"])
    assert sg == "hold"
    assert "拉高型" in reason or "涨停" in reason or "观望" in reason


# ---------------- 14章 基本面 ----------------


def test_pe_select_low_pe_buy(monkeypatch):
    """PE < 15 → buy。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"pe_ttm": 10.0})
    sg, reason = se.strategy_pe_select(ctx, se.DEFAULT_STRATEGY_PARAMS["pe_select"])
    assert sg == "buy"
    assert "低估值" in reason


def test_pe_select_high_pe_sell(monkeypatch):
    """PE > 50 → sell。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"pe_ttm": 60.0})
    sg, reason = se.strategy_pe_select(ctx, se.DEFAULT_STRATEGY_PARAMS["pe_select"])
    assert sg == "sell"
    assert "高估" in reason


def test_pe_select_negative_pe_sell(monkeypatch):
    """PE < 0 亏损 → sell。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"pe_ttm": -5.0})
    sg, reason = se.strategy_pe_select(ctx, se.DEFAULT_STRATEGY_PARAMS["pe_select"])
    assert sg == "sell"
    assert "亏损" in reason


def test_pe_select_no_data_hold(monkeypatch):
    """财务数据不可得 → hold。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: None)
    sg, reason = se.strategy_pe_select(ctx, se.DEFAULT_STRATEGY_PARAMS["pe_select"])
    assert sg == "hold"
    assert "不可得" in reason or "缺失" in reason


def test_roe_pe_high_roe_low_pe_buy(monkeypatch):
    """ROE ≥ 15 + PE < 25 → buy。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"roe": 20.0, "pe_ttm": 15.0})
    sg, reason = se.strategy_roe_pe(ctx, se.DEFAULT_STRATEGY_PARAMS["roe_pe"])
    assert sg == "buy"
    assert "优质" in reason


def test_roe_pe_low_roe_high_pe_sell(monkeypatch):
    """ROE < 5 + PE > 50 → sell。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"roe": 2.0, "pe_ttm": 80.0})
    sg, reason = se.strategy_roe_pe(ctx, se.DEFAULT_STRATEGY_PARAMS["roe_pe"])
    assert sg == "sell"
    assert "高估" in reason


def test_roe_pe_missing_data_hold(monkeypatch):
    """ROE 或 PE 缺失 → hold。"""
    df = _make_df(n=100, seed=42)
    ctx = _ctx(df, code="600519")
    monkeypatch.setattr(se, "_fetch_finance_safe", lambda c: {"roe": None, "pe_ttm": 15.0})
    sg, reason = se.strategy_roe_pe(ctx, se.DEFAULT_STRATEGY_PARAMS["roe_pe"])
    assert sg == "hold"
    assert "缺失" in reason


# ---------------- strategy_library.json 一致性 ----------------


def test_library_implemented_count():
    """策略大全应有 72 个已实现(73 总 - 1 未实现 T+0)。"""
    import json
    from pathlib import Path
    lib_path = Path(se.__file__).parent / "strategy_library.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    total = impl = 0
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                total += 1
                if st.get("implemented"):
                    impl += 1
    assert total == 73, f"策略总数应为 73,实际 {total}"
    assert impl == 72, f"已实现应为 72,实际 {impl}"


def test_library_new_strategies_have_engine_id():
    """新增 22 个策略都应在 library 中标 implemented=True + engine_id。"""
    import json
    from pathlib import Path
    lib_path = Path(se.__file__).parent / "strategy_library.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    # library id -> engine id 映射
    expected = {
        "高量柱": "high_volume", "看妖股": "demon_stock", "龙回头": "dragon_pullback",
        "压力支撑": "support_resistance", "区间交易": "range_trade",
        "plan_trade": "plan_trade", "trend_follow": "trend_follow",
        "pyramid": "pyramid", "stop_profit": "stop_profit",
        "pe_select": "pe_select", "bottom_ma": "bottom_ma",
        "top_weekly": "top_weekly", "top_monthly": "top_monthly",
        "zhuang_test": "zhuang_test", "zhuang_build": "zhuang_build",
        "zhuang_pull": "zhuang_pull", "zhuang_ship": "zhuang_ship",
        "zhuang_wash": "zhuang_wash", "zt_type": "zt_type",
        "zt_unsealed": "zt_unsealed", "zt_pull": "zt_pull",
        "roa_pe_筹码": "roe_pe",
    }
    found = {}
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                sid = st.get("id", "")
                if sid in expected:
                    found[sid] = (st.get("implemented", False), st.get("engine_id", ""))
    for lib_id, exp_engine_id in expected.items():
        assert lib_id in found, f"{lib_id} 不在 library 中"
        impl, engine_id = found[lib_id]
        assert impl is True, f"{lib_id} 应标 implemented=True"
        assert engine_id == exp_engine_id, f"{lib_id} engine_id 应为 {exp_engine_id},实际 {engine_id}"


def test_library_merged_strategies_have_engine_id():
    """已融入其他策略的 6 个策略也应标 implemented=True + engine_id。"""
    import json
    from pathlib import Path
    lib_path = Path(se.__file__).parent / "strategy_library.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    expected = {
        "抄底法": "bounce",
        "bottom_kline": "bottom",
        "bottom_accel": "bottom",
        "top_volume": "top",
        "top_accel": "top",
        "hotspot_select": "hotspot_select",
    }
    found = {}
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                sid = st.get("id", "")
                if sid in expected:
                    found[sid] = (st.get("implemented", False), st.get("engine_id", ""))
    for lib_id, exp_engine_id in expected.items():
        assert lib_id in found, f"{lib_id} 不在 library 中"
        impl, engine_id = found[lib_id]
        assert impl is True, f"{lib_id} 应标 implemented=True"
        assert engine_id == exp_engine_id, f"{lib_id} engine_id 应为 {exp_engine_id},实际 {engine_id}"


def test_library_unimplemented_count():
    """保留未实现的 1 个:T+0(需分钟数据,无法量化)。"""
    import json
    from pathlib import Path
    lib_path = Path(se.__file__).parent / "strategy_library.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    expected_unimpl = {"T+0"}
    actual_unimpl = set()
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                if not st.get("implemented", False):
                    actual_unimpl.add(st.get("id", ""))
    assert actual_unimpl == expected_unimpl, f"未实现策略不匹配,实际 {actual_unimpl}"
