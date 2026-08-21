"""stock_names.lookup_names 批量查名测试。"""

import sqlite3

import pytest

import stock_names as sn


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 stock_cache.db,只含 stock_names 表。"""
    db = tmp_path / "stock_cache.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE stock_names(
        query TEXT, code TEXT, name TEXT, ts REAL,
        PRIMARY KEY (query, name)
    )""")
    # 埋 3 条记录
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)", ("茅台", "600519", "茅台", 1.0))
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)", ("平安", "000001", "平安银行", 1.0))
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)", ("宁德", "300750", "宁德时代", 1.0))
    # name 为空的不应被返回
    conn.execute("INSERT INTO stock_names VALUES(?,?,?,?)", ("xxx", "999999", "", 1.0))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sn, "DB_PATH", db)
    return db


def test_lookup_names_basic(tmp_db):
    """已知代码应返回对应名称。"""
    m = sn.lookup_names(["600519", "000001", "300750"])
    assert m == {"600519": "茅台", "000001": "平安银行", "300750": "宁德时代"}


def test_lookup_names_empty_name_excluded(tmp_db):
    """name 为空的记录不应出现。"""
    m = sn.lookup_names(["999999"])
    assert m == {}


def test_lookup_names_unknown_code(tmp_db):
    """未缓存的 code 不出现在返回中。"""
    m = sn.lookup_names(["888888"])
    assert m == {}


def test_lookup_names_mixed(tmp_db):
    """混合:部分已知部分未知,只返回已知部分。"""
    m = sn.lookup_names(["600519", "888888", "000001"])
    assert m == {"600519": "茅台", "000001": "平安银行"}


def test_lookup_names_empty_input(tmp_db):
    """空列表返回空 dict。"""
    assert sn.lookup_names([]) == {}
