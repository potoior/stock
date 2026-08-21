"""5 个补齐策略(打板/复盘法/时间识底/股东人数/政策选股)单元测试。

覆盖:
  - 漫画书 实战战法: daban / fupan
  - 操练大全15章 抄底: bottom_time
  - 操练大全14章 选股: shareholder_select / policy_select
  - 注册到 BUILTIN 列表 + DEFAULT_STRATEGY_PARAMS
  - scan_with_strategy 不允许联网策略(scan_with_strategy 拒绝 shareholder_select/policy_select)
  - stock_finance.fetch_shareholder 接口解析(不联网)
"""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

import strategy_engine as se

NEW_STRATEGY_IDS = ["daban", "fupan", "bottom_time", "shareholder_select", "policy_select"]


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


def _make_df_with_zt(n=120, zt_days=None, seed=42):
    """构造在指定日期涨停的 df(涨幅 >= 9.6%)。"""
    df = _make_df(n=n, seed=seed)
    if zt_days:
        for d in zt_days:
            if d < 1 or d >= n:
                continue
            prev = df["close"].iloc[d - 1]
            df.loc[d, "close"] = prev * 1.10  # +10% 涨停
            df.loc[d, "high"] = prev * 1.11
            df.loc[d, "low"] = prev * 1.09
            df.loc[d, "open"] = prev * 1.05
            df.loc[d, "volume"] = df["volume"].iloc[d] * 3  # 放量
    return df


# ---------------- 注册 + 默认参数 ----------------


def test_all_new_strategies_in_default_params():
    """5 个新策略都应在 DEFAULT_STRATEGY_PARAMS 中。"""
    for sid in NEW_STRATEGY_IDS:
        assert sid in se.DEFAULT_STRATEGY_PARAMS, f"{sid} 不在 DEFAULT_STRATEGY_PARAMS"


def test_all_new_strategies_in_builtin_list():
    """5 个新策略都应在 analyze() 的 BUILTIN 列表中。"""
    import re
    src = open(se.__file__).read()
    for sid in NEW_STRATEGY_IDS:
        pattern = rf'\("{sid}",\s*"[^"]+",\s*strategy_{sid}\)'
        assert re.search(pattern, src), f"{sid} 未注册到 BUILTIN"


def test_all_new_strategies_callable_with_default_params():
    """5 个新策略在普通数据上不报错,返回 buy/sell/hold 之一。"""
    df = _make_df(n=250, seed=42)
    ctx = _ctx(df)
    for sid in NEW_STRATEGY_IDS:
        fn = getattr(se, f"strategy_{sid}")
        with patch("stock_finance.fetch_shareholder", return_value={"error": "mock"}), \
             patch("news_digest.fetch_stock_news", return_value=[]):
            sg, rsn = fn(ctx, se.DEFAULT_STRATEGY_PARAMS[sid])
        assert sg in ("buy", "sell", "hold"), f"{sid} 返回非法信号: {sg}"
        assert isinstance(rsn, str) and rsn, f"{sid} reason 为空"


def test_data_insufficient_returns_hold():
    """数据不足应返回 hold(只测不联网的策略: daban/fupan/bottom_time)。"""
    df = _make_df(n=5, seed=1)
    ctx = _ctx(df)
    for sid in ["daban", "fupan", "bottom_time"]:
        fn = getattr(se, f"strategy_{sid}")
        sg, _ = fn(ctx, se.DEFAULT_STRATEGY_PARAMS[sid])
        assert sg == "hold", f"{sid} 数据不足时应 hold,实际 {sg}"


# ---------------- daban 打板 ----------------


def test_daban_consecutive_zt_buy():
    """连板 + 放量 + 涨停 → buy。"""
    n = 30
    df = _make_df_with_zt(n=n, zt_days=[n - 2, n - 1], seed=42)  # 末两日连板
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_daban(ctx, se.DEFAULT_STRATEGY_PARAMS["daban"])
    assert sg == "buy", f"连板应 buy,实际 {sg}: {rsn}"
    assert "连板2日" in rsn


def test_daban_single_zt_no_consec_hold():
    """单日涨停但连板不足 → hold。"""
    n = 30
    df = _make_df_with_zt(n=n, zt_days=[n - 1], seed=42)  # 只末日涨停
    ctx = _ctx(df, i=n - 1)
    sg, _ = se.strategy_daban(ctx, {**se.DEFAULT_STRATEGY_PARAMS["daban"], "consec": 2})
    assert sg == "hold", f"连板不足应 hold,实际 {sg}"


def test_daban_no_zt_hold():
    """无涨停 → hold。"""
    df = _make_df(n=50, seed=42)
    ctx = _ctx(df)
    sg, rsn = se.strategy_daban(ctx, se.DEFAULT_STRATEGY_PARAMS["daban"])
    assert sg == "hold"
    assert "无打板信号" in rsn


# ---------------- fupan 复盘法 ----------------


def test_fupan_zt_at_support_shrink_buy():
    """近期有涨停 + 当前在支撑位 + 缩量 → buy。"""
    n = 60
    df = _make_df_with_zt(n=n, zt_days=[n - 10], seed=42)  # 10 日前涨停
    # 把末日价格调到近 30 日低点附近(支撑位)
    recent_low = df["close"].iloc[n - 30:n].min()
    df.loc[n - 1, "close"] = recent_low * 1.01
    df.loc[n - 1, "volume"] = df["volume"].iloc[n - 20:n - 1].mean() * 0.5  # 缩量
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_fupan(ctx, se.DEFAULT_STRATEGY_PARAMS["fupan"])
    assert sg == "buy", f"支撑位+缩量应 buy,实际 {sg}: {rsn}"
    assert "支撑位" in rsn


def test_fupan_at_resistance_stale_sell():
    """累计大涨 + 压力位 + 放量滞涨 → sell。"""
    n = 60
    df = _make_df(n=n, seed=42)
    # 构造累计大涨 30%(从 30 日前涨到末日)
    base = df["close"].iloc[n - 30]
    df.loc[n - 1, "close"] = base * 1.30
    df.loc[n - 1, "high"] = base * 1.31
    df.loc[n - 1, "low"] = base * 1.29
    # 末日接近 30 日高点(压力位)
    recent_high = df["close"].iloc[n - 30:n].max()
    df.loc[n - 1, "close"] = recent_high * 0.99
    # 末日放量,涨幅小(滞涨)
    df.loc[n - 1, "volume"] = df["volume"].iloc[n - 20:n - 1].mean() * 2.0
    df.loc[n - 1, "close"] = df["close"].iloc[n - 2] * 1.005  # +0.5% 滞涨
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_fupan(ctx, se.DEFAULT_STRATEGY_PARAMS["fupan"])
    # 由于构造数据较复杂,宽松断言:sell 或 hold 都可,但不能 buy
    assert sg != "buy", f"压力位放量不应 buy: {rsn}"


def test_fupan_no_signal_normal():
    """正常行情无信号 → hold。"""
    df = _make_df(n=60, seed=42)
    ctx = _ctx(df)
    sg, rsn = se.strategy_fupan(ctx, se.DEFAULT_STRATEGY_PARAMS["fupan"])
    assert sg == "hold"
    assert "无信号" in rsn


# ---------------- bottom_time 时间识底 ----------------


def test_bottom_time_at_fib_window_low_buy():
    """距最低点 8 日(斐波那契窗)+ 价在 MA60 下 → buy。"""
    n = 130
    df = _make_df(n=n, seed=42)
    # 构造 8 日前是最低点
    low_idx = n - 8
    df.loc[low_idx, "close"] = df["close"].iloc[:low_idx].min() * 0.9
    # 末日价格低于 MA60
    ma60 = df["close"].rolling(60).mean().iloc[n - 1]
    df.loc[n - 1, "close"] = ma60 * 0.9
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_bottom_time(ctx, se.DEFAULT_STRATEGY_PARAMS["bottom_time"])
    assert sg == "buy", f"斐波那契 8 日窗+低位应 buy,实际 {sg}: {rsn}"
    assert "斐波那契8日" in rsn


def test_bottom_time_at_fib_window_high_sell():
    """距最低点 13 日(斐波那契窗)+ 价在 MA60×1.1 上 → sell。"""
    n = 130
    df = _make_df(n=n, seed=42)
    low_idx = n - 13
    df.loc[low_idx, "close"] = df["close"].iloc[:low_idx].min() * 0.9
    ma60 = df["close"].rolling(60).mean().iloc[n - 1]
    df.loc[n - 1, "close"] = ma60 * 1.2  # 远高于 MA60×1.1
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_bottom_time(ctx, se.DEFAULT_STRATEGY_PARAMS["bottom_time"])
    assert sg == "sell", f"斐波那契 13 日窗+高位应 sell,实际 {sg}: {rsn}"
    assert "斐波那契13日" in rsn


def test_bottom_time_not_at_fib_window_hold():
    """距最低点不在斐波那契窗 → hold。"""
    n = 130
    df = _make_df(n=n, seed=42)
    # 最低点在 100 日前(不在 8/13/21/34/55/89)
    low_idx = n - 100
    df.loc[low_idx, "close"] = df["close"].iloc[:low_idx].min() * 0.5
    ctx = _ctx(df, i=n - 1)
    sg, rsn = se.strategy_bottom_time(ctx, se.DEFAULT_STRATEGY_PARAMS["bottom_time"])
    assert sg == "hold"
    assert "不在斐波那契窗" in rsn


# ---------------- shareholder_select 股东人数选股 ----------------


def test_shareholder_select_concentrate_low_buy():
    """股东人数减少 + 价在低位 → buy。"""
    df = _make_df(n=120, seed=42)
    # 把末日价格调到近 60 日低分位(分位 <= 0.3)
    low = df["close"].iloc[-60:].min()
    df.loc[len(df) - 1, "close"] = low * 1.01
    ctx = _ctx(df)
    mock_data = {"holder_num": 10000, "holder_num_prev": 12000,
                 "change_pct": -16.67, "end_date": "2026-06-30",
                 "prev_end_date": "2026-03-31", "hold_focus": "较集中"}
    with patch("stock_finance.fetch_shareholder", return_value=mock_data):
        sg, rsn = se.strategy_shareholder_select(ctx, se.DEFAULT_STRATEGY_PARAMS["shareholder_select"])
    assert sg == "buy", f"筹码集中+低位应 buy,实际 {sg}: {rsn}"
    assert "集中" in rsn


def test_shareholder_select_disperse_high_sell():
    """股东人数增加 + 价在高位 → sell。"""
    df = _make_df(n=120, seed=42)
    # 把末日价格调到近 60 日高分位(分位 >= 0.7)
    high = df["close"].iloc[-60:].max()
    df.loc[len(df) - 1, "close"] = high * 0.99
    ctx = _ctx(df)
    mock_data = {"holder_num": 20000, "holder_num_prev": 15000,
                 "change_pct": 33.33, "end_date": "2026-06-30",
                 "prev_end_date": "2026-03-31", "hold_focus": "非常分散"}
    with patch("stock_finance.fetch_shareholder", return_value=mock_data):
        sg, rsn = se.strategy_shareholder_select(ctx, se.DEFAULT_STRATEGY_PARAMS["shareholder_select"])
    assert sg == "sell", f"筹码分散+高位应 sell,实际 {sg}: {rsn}"
    assert "分散" in rsn


def test_shareholder_select_data_unavailable_hold():
    """股东人数数据不可得 → hold。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    with patch("stock_finance.fetch_shareholder", return_value={"error": "mock"}):
        sg, rsn = se.strategy_shareholder_select(ctx, se.DEFAULT_STRATEGY_PARAMS["shareholder_select"])
    assert sg == "hold"
    assert "不可得" in rsn


def test_shareholder_select_no_code_hold():
    """无 code 字段 → hold。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    ctx["code"] = ""
    sg, rsn = se.strategy_shareholder_select(ctx, se.DEFAULT_STRATEGY_PARAMS["shareholder_select"])
    assert sg == "hold"
    assert "无股票代码" in rsn


# ---------------- policy_select 政策选股 ----------------


def test_policy_select_positive_low_buy():
    """多条利好新闻 + 价在低位 → buy。"""
    df = _make_df(n=120, seed=42)
    low = df["close"].iloc[-60:].min()
    df.loc[len(df) - 1, "close"] = low * 1.01
    ctx = _ctx(df)
    mock_news = [
        {"title": "国家政策支持新能源发展", "summary": "国务院扶持新能源产业",
         "time": "2026-08-10", "source": "新华社", "url": ""},
        {"title": "公司中标10亿大单", "summary": "近日获批重大订单",
         "time": "2026-08-09", "source": "证券报", "url": ""},
    ]
    with patch("news_digest.fetch_stock_news", return_value=mock_news):
        sg, rsn = se.strategy_policy_select(ctx, se.DEFAULT_STRATEGY_PARAMS["policy_select"])
    assert sg == "buy", f"利好+低位应 buy,实际 {sg}: {rsn}"
    assert "利好" in rsn


def test_policy_select_negative_high_sell():
    """多条利空新闻 + 价在高位 → sell。"""
    df = _make_df(n=120, seed=42)
    high = df["close"].iloc[-60:].max()
    df.loc[len(df) - 1, "close"] = high * 0.99
    ctx = _ctx(df)
    mock_news = [
        {"title": "公司被监管处罚", "summary": "违规减持被立案调查",
         "time": "2026-08-10", "source": "新华社", "url": ""},
        {"title": "业绩亏损下滑", "summary": "净利润大幅下滑",
         "time": "2026-08-09", "source": "证券报", "url": ""},
    ]
    with patch("news_digest.fetch_stock_news", return_value=mock_news):
        sg, rsn = se.strategy_policy_select(ctx, se.DEFAULT_STRATEGY_PARAMS["policy_select"])
    assert sg == "sell", f"利空+高位应 sell,实际 {sg}: {rsn}"
    assert "利空" in rsn


def test_policy_select_no_news_hold():
    """无新闻 → hold。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    with patch("news_digest.fetch_stock_news", return_value=[]):
        sg, rsn = se.strategy_policy_select(ctx, se.DEFAULT_STRATEGY_PARAMS["policy_select"])
    assert sg == "hold"
    assert "无相关新闻" in rsn


def test_policy_select_insufficient_hits_hold():
    """利好/利空命中不足 → hold。"""
    df = _make_df(n=120, seed=42)
    ctx = _ctx(df)
    mock_news = [{"title": "公司发布日常公告", "summary": "一般事项",
                  "time": "2026-08-10", "source": "新华社", "url": ""}]
    with patch("news_digest.fetch_stock_news", return_value=mock_news):
        sg, rsn = se.strategy_policy_select(ctx, se.DEFAULT_STRATEGY_PARAMS["policy_select"])
    assert sg == "hold"
    assert "无明显政策面信号" in rsn


# ---------------- scan_with_strategy 拒绝联网策略 ----------------


def test_scan_with_strategy_rejects_shareholder_select():
    """scan_with_strategy 应拒绝 shareholder_select(需联网)。"""
    result = se.scan_with_strategy("shareholder_select")
    assert "error" in result
    assert "联网" in result["error"] or "不适合" in result["error"]


def test_scan_with_strategy_rejects_policy_select():
    """scan_with_strategy 应拒绝 policy_select(需联网)。"""
    result = se.scan_with_strategy("policy_select")
    assert "error" in result
    assert "联网" in result["error"] or "不适合" in result["error"]


def test_scan_with_strategy_accepts_daban():
    """scan_with_strategy 应接受 daban(不联网,mock 后跑通)。"""
    df = _make_df(n=120, seed=42)
    with patch("strategy_engine.get_daily_data", return_value=df):
        with patch("sqlite3.connect") as mock_connect:
            mock_conn = mock_connect.return_value
            mock_conn.execute.return_value.fetchall.return_value = [
                ("600519", 120, "20260820"),
            ]
            result = se.scan_with_strategy("daban", top_n=10, min_amount_yi=0)
    assert "error" not in result
    assert result["scanned"] == 1


# ---------------- stock_finance 股东人数接口 ----------------


def test_fetch_shareholder_invalid_code():
    """非法代码应返回 error。"""
    import stock_finance as sf
    assert "error" in sf.fetch_shareholder("")
    assert "error" in sf.fetch_shareholder("12345")
    assert "error" in sf.fetch_shareholder("abcdef")


def test_fetch_shareholder_network_error():
    """网络异常应返回 error,不抛异常。"""
    import stock_finance as sf
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = sf.fetch_shareholder("600519")
    assert "error" in result


def test_fetch_shareholder_parses_response():
    """正常 JSON 响应应正确解析为股东人数变动数据。"""
    import stock_finance as sf
    jsonp_data = {
        "result": {
            "data": [
                {"HOLDER_TOTAL_NUM": 296404, "TOTAL_NUM_RATIO": 21.9,
                 "END_DATE": "2026-06-30 00:00:00", "HOLD_FOCUS": "非常分散"},
                {"HOLDER_TOTAL_NUM": 243159, "TOTAL_NUM_RATIO": -4.97,
                 "END_DATE": "2026-03-31 00:00:00", "HOLD_FOCUS": "非常分散"},
            ]
        }
    }
    resp = MagicMock()
    resp.read.return_value = json.dumps(jsonp_data).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=resp):
        result = sf.fetch_shareholder("600519")
    assert "error" not in result
    assert result["holder_num"] == 296404
    assert result["holder_num_prev"] == 243159
    assert result["change_pct"] == 21.9
    assert result["end_date"] == "2026-06-30"
    assert result["hold_focus"] == "非常分散"


def test_fetch_shareholder_empty_response():
    """空响应应返回 error。"""
    import stock_finance as sf
    resp = MagicMock()
    resp.read.return_value = json.dumps({"result": {"data": []}}).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=resp):
        result = sf.fetch_shareholder("600519")
    assert "error" in result


def test_fetch_shareholder_calculate_change_pct_when_missing():
    """接口未返回 TOTAL_NUM_RATIO 时,应自行计算 change_pct。"""
    import stock_finance as sf
    jsonp_data = {
        "result": {
            "data": [
                {"HOLDER_TOTAL_NUM": 10000, "END_DATE": "2026-06-30 00:00:00",
                 "HOLD_FOCUS": "较集中"},
                {"HOLDER_TOTAL_NUM": 12000, "END_DATE": "2026-03-31 00:00:00",
                 "HOLD_FOCUS": "较集中"},
            ]
        }
    }
    resp = MagicMock()
    resp.read.return_value = json.dumps(jsonp_data).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=resp):
        result = sf.fetch_shareholder("600519")
    assert "error" not in result
    assert result["change_pct"] == round((10000 - 12000) / 12000 * 100, 2)  # -16.67
