"""stock_finance 财务数据获取单测。

不联网,所有外部接口都 mock。
"""

import time

import stock_finance as sf

# ============ secid / secucode 编码 ============


def test_eastmoney_secid():
    """6位代码 → 东财 secid 编码。"""
    assert sf._eastmoney_secid("600519") == "1.600519"  # 沪市
    assert sf._eastmoney_secid("601318") == "1.601318"  # 沪市
    assert sf._eastmoney_secid("688981") == "1.688981"  # 科创板
    assert sf._eastmoney_secid("000001") == "0.000001"  # 深市主板
    assert sf._eastmoney_secid("300750") == "0.300750"  # 创业板
    assert sf._eastmoney_secid("002594") == "0.002594"  # 中小板


def test_eastmoney_secucode():
    """6位代码 → 东财 SECUCODE。"""
    assert sf._eastmoney_secucode("600519") == "600519.SH"
    assert sf._eastmoney_secucode("300750") == "300750.SZ"


# ============ fetch_finance 输入校验 ============


def test_fetch_finance_invalid_code_empty():
    """空代码应返错误。"""
    r = sf.fetch_finance("")
    assert "error" in r
    assert "6 位" in r["error"]


def test_fetch_finance_invalid_code_non_digit():
    """非数字代码应返错误。"""
    r = sf.fetch_finance("茅台")
    assert "error" in r


def test_fetch_finance_invalid_code_wrong_length():
    """长度不对的代码应返错误。"""
    r = sf.fetch_finance("60051")
    assert "error" in r
    r = sf.fetch_finance("6005190")
    assert "error" in r


# ============ 缓存读写 ============


def test_cache_put_get(tmp_path, monkeypatch):
    """缓存写入后能读出,过期后返 None。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    sf._cache_put("600519", {"code": "600519", "name": "茅台"}, None)
    rt, rep = sf._cache_get("600519")
    assert rt is not None
    assert rt["code"] == "600519"
    assert rt["name"] == "茅台"
    assert rep is None  # 未写 rep


def test_cache_put_both_layers(tmp_path, monkeypatch):
    """rt + rep 分层缓存,各自独立 TTL。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    sf._cache_put("600519", {"pe": 18}, {"roe": 16})
    rt, rep = sf._cache_get("600519")
    assert rt == {"pe": 18}
    assert rep == {"roe": 16}


def test_cache_get_miss(tmp_path, monkeypatch):
    """缓存不存在返 (None, None)。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    rt, rep = sf._cache_get("000000")
    assert rt is None
    assert rep is None


def test_cache_get_expired(tmp_path, monkeypatch):
    """rt 缓存过期(1 天),rep 缓存未过期(30 天)。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    # 写入:rt 用过去时间戳(已过期),rep 用新时间戳(未过期)
    sf._cache_put("600519", {"pe": 18}, {"roe": 16})
    # 手动改 rt_ts 为 2 天前
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE stock_finance SET rt_ts = ? WHERE code = ?", (int(time.time()) - 86400 * 2, "600519"))
    conn.commit()
    conn.close()
    rt, rep = sf._cache_get("600519")
    assert rt is None  # rt 过期
    assert rep is not None  # rep 未过期
    assert rep["roe"] == 16


def test_cache_rt_expired_rep_fresh_partial_fetch(tmp_path, monkeypatch):
    """rt 过期 rep 未过期:只重拉 rt,不重拉 rep(节省 HTTP)。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    # 写入 rep 缓存
    sf._cache_put("600519", None, {"roe": 16, "report_name": "2026中报"})
    # 让 rt 过期:不写 rt(_cache_put 只写 rep 时 rt_ts 是 None)
    fetch_calls = []
    def fake_rt(c):
        fetch_calls.append("rt")
        return {"pe": 20, "name": "茅台"}
    def fake_rep(c):
        fetch_calls.append("rep")
        raise AssertionError("rep 未过期不应重拉")
    monkeypatch.setattr(sf, "_fetch_realtime_finance", fake_rt)
    monkeypatch.setattr(sf, "_fetch_report_finance", fake_rep)

    r = sf.fetch_finance("600519")
    assert fetch_calls == ["rt"]  # 只拉了 rt
    assert r["pe"] == 20
    assert r["roe"] == 16  # rep 从缓存


# ============ fmt_finance 格式化 ============


def test_fmt_finance_error():
    """错误数据格式化为 ❌。"""
    out = sf.fmt_finance({"error": "代码不存在"})
    assert "❌" in out
    assert "代码不存在" in out


def test_fmt_finance_full():
    """完整财务数据应格式化为 markdown,所有字段都在。"""
    data = {
        "code": "600519",
        "name": "贵州茅台",
        "pe_dynamic": 18.36,
        "pe_static": 19.86,
        "pe_ttm": 20.08,
        "pb": 6.51,
        "total_mv": 1.63e12,
        "float_mv": 1.63e12,
        "bps": 200.99,
        "report_name": "2026中报",
        "eps": 35.57,
        "roe": 16.75,
        "gross_margin": 89.56,
        "net_margin": 50.75,
        "revenue": 9.23e10,
        "net_profit": 4.45e10,
        "revenue_yoy": 1.30,
        "profit_yoy": -1.95,
        "debt_ratio": 15.19,
    }
    out = sf.fmt_finance(data)
    assert "贵州茅台" in out
    assert "600519" in out
    assert "18.36" in out  # PE
    assert "16300.00" in out  # 总市值(亿)
    assert "16.75%" in out  # ROE
    assert "89.56%" in out  # 毛利率
    assert "2026中报" in out
    assert "-1.95%" in out  # 净利润同比


def test_fmt_finance_missing_fields():
    """缺字段时显示 '-'。"""
    data = {"code": "000001", "name": "平安银行"}
    out = sf.fmt_finance(data)
    assert "平安银行" in out
    # 缺失字段以 - 显示
    assert "-" in out


# ============ fetch_finance mock 集成 ============


def test_fetch_finance_uses_cache_first(tmp_path, monkeypatch):
    """两层缓存都命中时不应调网络。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    # 预填两层缓存
    sf._cache_put("600519", {"code": "600519", "name": "茅台"}, {"roe": 16})

    def no_call(*a, **kw):
        raise AssertionError("不应调网络")
    monkeypatch.setattr(sf, "_fetch_realtime_finance", no_call)
    monkeypatch.setattr(sf, "_fetch_report_finance", no_call)

    r = sf.fetch_finance("600519")
    assert r["name"] == "茅台"
    assert r["roe"] == 16


def test_fetch_finance_merges_and_caches(tmp_path, monkeypatch):
    """缓存未命中时调网络 + 合并 + 写缓存。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)

    rt = {"code": "600519", "name": "茅台", "pe_dynamic": 18.36, "pb": 6.51}
    rep = {"report_name": "2026中报", "roe": 16.75, "net_margin": 50.75}
    monkeypatch.setattr(sf, "_fetch_realtime_finance", lambda c: rt)
    monkeypatch.setattr(sf, "_fetch_report_finance", lambda c: rep)

    r = sf.fetch_finance("600519")
    assert r["name"] == "茅台"
    assert r["pe_dynamic"] == 18.36
    assert r["roe"] == 16.75
    assert r["report_name"] == "2026中报"
    # 两层缓存都已写入
    cached_rt, cached_rep = sf._cache_get("600519")
    assert cached_rt is not None
    assert cached_rt["pe_dynamic"] == 18.36
    assert cached_rep is not None
    assert cached_rep["roe"] == 16.75


def test_fetch_finance_all_failed(tmp_path, monkeypatch):
    """两个接口都失败应返 error。"""
    db = tmp_path / "test.db"
    monkeypatch.setattr(sf, "DB_PATH", db)
    monkeypatch.setattr(sf, "_fetch_realtime_finance", lambda c: {})
    monkeypatch.setattr(sf, "_fetch_report_finance", lambda c: {})

    r = sf.fetch_finance("600519")
    assert "error" in r
    assert "600519" in r["error"]
