"""news_digest.fetch_stock_news 单元测试(个股新闻抓取)。

不联网,mock urllib.request.urlopen 验证解析逻辑。
"""

import json
from unittest.mock import MagicMock, patch

import news_digest


def _mock_response_jsonp(data):
    """构造 MagicMock 模拟东财 JSONP 响应(cb({...}))。"""
    resp = MagicMock()
    body = "cb(" + json.dumps(data, ensure_ascii=False) + ")"
    resp.read.return_value = body.encode("utf-8")
    return resp


def test_fetch_stock_news_invalid_code_returns_empty():
    """无效代码应返回空列表。"""
    assert news_digest.fetch_stock_news("") == []
    assert news_digest.fetch_stock_news("12345") == []  # 5 位
    assert news_digest.fetch_stock_news("abcdef") == []  # 非数字


def test_fetch_stock_news_network_error_returns_empty():
    """网络异常时返回空列表,不抛异常。"""
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = news_digest.fetch_stock_news("301189")
    assert result == []


def test_fetch_stock_news_parses_jsonp():
    """正常 JSONP 响应应正确解析为新闻列表。"""
    # mock stock_names 模块 + DB 查询返回股票名
    fake_rows = [("奥尼电子", "深圳奥尼电子股份有限公司")]

    jsonp_data = {
        "result": {
            "cmsArticleWebOld": [
                {
                    "date": "2026-08-13 13:03:00",
                    "title": "<em>奥尼电子</em>：英伟达目前不是公司客户",
                    "content": "<em>奥尼电子</em>(301189)8月13日互动平台表示...",
                    "mediaName": "南方财经网",
                    "url": "http://finance.eastmoney.com/a/123.html",
                },
                {
                    "date": "2026-08-10 10:00:00",
                    "title": "无关列表新闻",
                    "content": "一些无关内容",
                    "mediaName": "证券时报",
                    "url": "http://finance.eastmoney.com/a/456.html",
                },
            ]
        }
    }

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = fake_rows

    with patch("urllib.request.urlopen", return_value=_mock_response_jsonp(jsonp_data)), \
         patch("sqlite3.connect", return_value=fake_conn):
        result = news_digest.fetch_stock_news("301189", num=10, strict=True)

    # strict 过滤后只保留 1 条(提到奥尼电子的)
    assert len(result) == 1
    n = result[0]
    assert "奥尼电子" in n["title"]
    assert "<em>" not in n["title"]  # <em> 标签已清理
    assert "<em>" not in n["summary"]
    assert n["time"] == "2026-08-13 13:03"
    assert n["source"] == "南方财经网"
    assert n["url"] == "http://finance.eastmoney.com/a/123.html"


def test_fetch_stock_news_strict_false_keeps_all():
    """strict=False 应保留所有搜索结果(包括无关列表新闻)。"""
    fake_rows = [("奥尼电子", "深圳奥尼电子股份有限公司")]
    jsonp_data = {
        "result": {
            "cmsArticleWebOld": [
                {"date": "2026-08-13 13:03", "title": "奥尼电子新闻", "content": "内容含奥尼电子",
                 "mediaName": "源1", "url": "http://1"},
                {"date": "2026-08-10 10:00", "title": "无关新闻", "content": "无关内容",
                 "mediaName": "源2", "url": "http://2"},
            ]
        }
    }
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = fake_rows
    with patch("urllib.request.urlopen", return_value=_mock_response_jsonp(jsonp_data)), \
         patch("sqlite3.connect", return_value=fake_conn):
        result = news_digest.fetch_stock_news("301189", num=10, strict=False)
    assert len(result) == 2


def test_fetch_stock_news_invalid_json_returns_empty():
    """JSON 解析失败时返回空列表。"""
    resp = MagicMock()
    resp.read.return_value = b"not a valid jsonp"
    with patch("urllib.request.urlopen", return_value=resp), \
         patch("sqlite3.connect", side_effect=Exception("no db")):
        result = news_digest.fetch_stock_news("301189")
    assert result == []


def test_fetch_stock_news_truncates_summary():
    """summary 应截断到 120 字。"""
    long_summary = "奥尼电子" + "x" * 200  # 远超 120 字
    fake_rows = [("奥尼电子", "")]
    jsonp_data = {
        "result": {
            "cmsArticleWebOld": [
                {"date": "2026-08-13", "title": "奥尼电子新闻", "content": long_summary,
                 "mediaName": "源", "url": ""},
            ]
        }
    }
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = fake_rows
    with patch("urllib.request.urlopen", return_value=_mock_response_jsonp(jsonp_data)), \
         patch("sqlite3.connect", return_value=fake_conn):
        result = news_digest.fetch_stock_news("301189", num=5)
    assert len(result) == 1
    assert len(result[0]["summary"]) <= 120


def test_fetch_stock_news_num_limit():
    """num 参数应限制返回条数。"""
    fake_rows = [("奥尼电子", "")]
    items = []
    for i in range(10):
        items.append({
            "date": "2026-08-13",
            "title": f"奥尼电子新闻{i}",
            "content": "奥尼电子",
            "mediaName": "源",
            "url": "",
        })
    jsonp_data = {"result": {"cmsArticleWebOld": items}}
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchall.return_value = fake_rows
    with patch("urllib.request.urlopen", return_value=_mock_response_jsonp(jsonp_data)), \
         patch("sqlite3.connect", return_value=fake_conn):
        result = news_digest.fetch_stock_news("301189", num=3)
    assert len(result) == 3
