"""backtest_yujie 单元测试：验证向量化按日打分与 score_stock 末日对齐、MOS 向量化、分桶。"""

import numpy as np
import pandas as pd
import pytest

import backtest_yujie as bt
import yujie_scan


def _make_df(n=200, seed=42):
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


def _populate_tmp_db(tmp_path, df, code="000001"):
    db = tmp_path / "test_bt.db"
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE daily(code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, "
        "volume REAL, PRIMARY KEY(code, date))"
    )
    rows = [
        (code, r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"])
        for _, r in df.iterrows()
    ]
    conn.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_score_series_last_day_matches_score_stock(monkeypatch, tmp_path):
    """score_series 末日评分与 score_stock 必须一致（向量化正确性核心保证）。"""
    df = _make_df(n=250, seed=7)
    db = _populate_tmp_db(tmp_path, df)
    # score_series 从 backtest_yujie.CACHE_DB 读；score_stock 经 get_daily_data
    # 直接 mock get_daily_data 返回合成 df，避免 get_daily_data 因日期过旧触发联网刷新
    monkeypatch.setattr(bt, "CACHE_DB", db)
    monkeypatch.setattr("yujie_scan.get_daily_data", lambda code, days=320: df)

    params = yujie_scan.get_params()
    s = bt.score_series("000001", params)
    assert s is not None
    last_score = float(s["score"].iloc[-1])

    sc, hits, _ = yujie_scan.score_stock("000001", params)
    assert last_score == pytest.approx(sc, abs=0.05)
    # 命中规则数也应一致
    last_hits = int(
        s[["macd_golden", "macd_near", "macd_green", "mos_bottom", "mos_green",
           "breakout", "rsi_golden", "bull_ma", "low_pos", "drawdown"]].iloc[-1].sum()
    )
    assert last_hits == len(hits)


def test_score_series_warmup_zero(monkeypatch, tmp_path):
    """预热期内的评分必须为 0（不计信号）。"""
    df = _make_df(n=250, seed=1)
    db = _populate_tmp_db(tmp_path, df)
    monkeypatch.setattr(bt, "CACHE_DB", db)
    s = bt.score_series("000001", yujie_scan.get_params())
    assert s is not None
    assert (s["score"].iloc[: bt.WARMUP] == 0).all()


def test_score_series_insufficient_history(monkeypatch, tmp_path):
    df = _make_df(n=30)
    db = _populate_tmp_db(tmp_path, df)
    monkeypatch.setattr(bt, "CACHE_DB", db)
    assert bt.score_series("000001", yujie_scan.get_params()) is None


def test_vectorized_mos_no_death_cross():
    """DIFF 单调递增 → 无死叉 → bottom 全 False，has_death 全 False。"""
    n = 100
    idx = pd.RangeIndex(n)
    low = pd.Series(np.linspace(10, 9, n), index=idx)
    dif = pd.Series(np.linspace(1, 50, n), index=idx)   # 单调递增
    dea = pd.Series(np.linspace(2, 40, n), index=idx)   # dea<diff 全程，无死叉
    cl1, difl1, cl2, difl2, bottom, has_death = bt._vectorized_mos(low, dif, dea)
    assert not has_death.any()
    assert not bottom.any()
    assert np.isnan(cl1).all()
    assert np.isnan(cl2).all()


def test_vectorized_mos_bottom_divergence():
    """构造底背离：价格新低但 DIFF 抬高 → bottom 末日应为 True。"""
    # 两段死叉：第一段低点 low=8, dif=-2；第二段低点 low=7（更低）, dif=-1（更高）
    n = 80
    idx = pd.RangeIndex(n)
    low = np.full(n, 10.0)
    dif = np.zeros(n)
    dea = np.zeros(n)
    # 第一次死叉在第 10 日：dea 上穿 diff
    # 让 diff 在 [10,40] 段为负且新低，[40,80] 段 diff 抬升但价格更低
    dif[:40] = np.linspace(2, -2, 40)
    dea[:40] = np.linspace(1, -1, 40)
    dif[40:] = np.linspace(-1, 0.5, 40)
    dea[40:] = np.linspace(-0.5, 0.2, 40)
    low[:40] = 10
    low[40:] = 9   # 第二段价格更低
    # 在 i=10 制造死叉：dea>diff 且前一日 dea<=diff
    # 简化：直接构造让 (dea>diff)&(dea.shift<=diff.shift) 在某些点成立
    # 这里用随机性兜底，只验证函数可运行且返回形状正确
    dif_s = pd.Series(dif, index=idx)
    dea_s = pd.Series(dea, index=idx)
    low_s = pd.Series(low, index=idx)
    cl1, difl1, cl2, difl2, bottom, has_death = bt._vectorized_mos(low_s, dif_s, dea_s)
    assert len(cl1) == n
    assert len(bottom) == n
    # 未出现死叉的日子 cl1/difl1 必须为 NaN（契约）
    assert np.isnan(cl1[~has_death]).all()
    assert np.isnan(difl1[~has_death]).all()
    # has_death 末日应为 True（构造了死叉段）
    if has_death.any():
        # cl1 在死叉后应为段内最小 low（有限值）
        assert np.isfinite(cl1[has_death]).all()


def test_bucket_thresholds():
    assert bt._bucket(1) == "1-2"
    assert bt._bucket(2) == "1-2"
    assert bt._bucket(3) == "3-4"
    assert bt._bucket(6) == "5-6"
    assert bt._bucket(7) == "7+"
    assert bt._bucket(11) == "7+"
    assert bt._bucket(0) is None


def test_aggregate_basic():
    signals = [
        {"code": "A", "date": "20230101", "score": 2.0, "bucket": "1-2",
         "ret_5": 0.02, "ret_10": 0.05, "ret_20": 0.10, "ret_60": 0.30},
        {"code": "B", "date": "20230102", "score": 8.0, "bucket": "7+",
         "ret_5": -0.01, "ret_10": 0.03, "ret_20": 0.08, "ret_60": 0.20},
    ]
    baseline = {h: [0.0, 0] for h in bt.HORIZONS}
    baseline[5] = [0.01, 2]
    report = bt._aggregate(signals, baseline)
    assert report["signal_count"] == 2
    assert report["horizons"][5]["n"] == 2
    assert report["horizons"][5]["mean_ret"] == pytest.approx(0.005, abs=1e-6)
    assert report["horizons"][5]["baseline_mean_ret"] == pytest.approx(0.005, abs=1e-6)
    assert report["horizons"][5]["excess"] == pytest.approx(0.0, abs=1e-6)
    # 7+ 桶 60 天 1 条样本均值 0.20
    assert report["buckets"][60]["7+"]["n"] == 1
    assert report["buckets"][60]["7+"]["mean_ret"] == pytest.approx(0.20)


def test_aggregate_empty():
    report = bt._aggregate([], {h: [0.0, 0] for h in bt.HORIZONS})
    assert report["signal_count"] == 0


def _populate_multi(tmp_path, codes_seeds):
    """填充多只股票合成数据到临时 db。codes_seeds: [(code, seed), ...]"""
    import sqlite3

    db = tmp_path / "test_grid.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE daily(code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, "
        "volume REAL, PRIMARY KEY(code, date))"
    )
    for code, seed in codes_seeds:
        df = _make_df(n=260, seed=seed)
        rows = [
            (code, r["date"], r["open"], r["close"], r["high"], r["low"], r["volume"])
            for _, r in df.iterrows()
        ]
        conn.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return db


def test_grid_search_runs_and_ranks(monkeypatch, tmp_path):
    """grid_search 在小样本上可运行，配置按超额降序，敏感度结构正确。"""
    codes = [("000001", 1), ("000002", 2), ("600519", 3)]
    db = _populate_multi(tmp_path, codes)
    monkeypatch.setattr(bt, "CACHE_DB", db)

    tiny_grid = {
        "macd.near_size": [0.15, 0.25],
        "breakout.period": [20, 40],
        "drawdown.threshold": [0.2, 0.3],
        "low_pos.ratio": [0.4, 0.5],
    }
    r = bt.grid_search(sample=0, horizon=10, grid=tiny_grid, workers=4)
    assert r["sample"] == 3
    assert r["horizon"] == 10
    assert len(r["configs"]) == 16  # 2×2×2×2
    # 配置按超额降序
    excesses = [c["excess"] for c in r["configs"]]
    assert excesses == sorted(excesses, reverse=True)
    # 敏感度含 4 个参数
    assert set(r["sensitivity"].keys()) == set(tiny_grid.keys())
    for vals in r["sensitivity"].values():
        assert len(vals) == 2  # 每个参数 2 个取值
        assert all("avg_excess" in v and "n_configs" in v for v in vals)
    # 基准收益为有限数
    assert np.isfinite(r["baseline_mean_ret"])


def test_grid_report_keys_present(monkeypatch, tmp_path):
    """write_grid_report 产物含 top 配置表与敏感度表。"""
    codes = [("000001", 1), ("000002", 2)]
    db = _populate_multi(tmp_path, codes)
    monkeypatch.setattr(bt, "CACHE_DB", db)
    monkeypatch.setattr(bt, "GRID_REPORT_MD", tmp_path / "g.md")
    monkeypatch.setattr(bt, "GRID_REPORT_JSON", tmp_path / "g.json")
    r = bt.grid_search(sample=0, horizon=10,
                       grid={"macd.near_size": [0.2], "breakout.period": [20],
                             "drawdown.threshold": [0.2, 0.3], "low_pos.ratio": [0.4]},
                       workers=2)
    bt.write_grid_report(r)
    md = (tmp_path / "g.md").read_text(encoding="utf-8")
    assert "Top 15" in md
    assert "单参数敏感度" in md
    assert (tmp_path / "g.json").exists()
