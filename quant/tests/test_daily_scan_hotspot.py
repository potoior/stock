"""daily_scan.scan_hotspot_stocks 单元测试(市场热点选股 hotspot_select)。

不联网,mock urllib.request.urlopen 验证解析逻辑。
"""

import json
from unittest.mock import MagicMock, patch

import daily_scan


def _mock_response(data):
    """构造 MagicMock 模拟 urllib response。"""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode("utf-8")
    return resp


def test_scan_hotspot_returns_empty_on_failure():
    """网络异常时返回空列表,不抛异常。"""
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = daily_scan.scan_hotspot_stocks()
    assert result == []


def test_scan_hotspot_parses_top_sectors():
    """正常响应时返回 top_sectors × top_stocks_per_sector 条记录。"""
    # 板块接口响应:概念(t=2)+行业(t=3)各 2 个
    sector_resp = {
        "data": {
            "diff": [
                {"f12": "BK0896", "f14": "白酒", "f3": 5.2},
                {"f12": "BK0475", "f14": "医药", "f3": 3.1},
            ]
        }
    }
    # 成分股响应:每板块 2 只
    members_resp = {
        "data": {
            "diff": [
                {"f12": "600519", "f14": "贵州茅台", "f3": 2.5, "f6": 1.2e9},
                {"f12": "000858", "f14": "五粮液", "f3": 1.8, "f6": 8e8},
            ]
        }
    }
    # urlopen 依次被调用:板块接口 2 次 + 每个板块成分股 2 次 = 4 次
    responses = [_mock_response(sector_resp), _mock_response(sector_resp),
                 _mock_response(members_resp), _mock_response(members_resp),
                 _mock_response(members_resp), _mock_response(members_resp)]
    with patch("urllib.request.urlopen", side_effect=responses):
        result = daily_scan.scan_hotspot_stocks(top_sectors=2, top_stocks_per_sector=2)
    # 至少返回 2 个板块 × 2 只 = 4 条
    assert len(result) >= 4
    # 第一条应是涨幅最高板块(白酒 5.2%)的成分股
    assert result[0]["sector"] == "白酒"
    assert result[0]["sector_code"] == "BK0896"
    assert result[0]["code"] == "600519"
    assert result[0]["name"] == "贵州茅台"
    assert result[0]["pct"] == 2.5
    assert result[0]["amount_yi"] == 12.0  # 1.2e9 / 1e8


def test_scan_hotspot_filters_invalid_codes():
    """过滤无效代码(非 6 位数字)。"""
    sector_resp = {"data": {"diff": [{"f12": "BK001", "f14": "测试", "f3": 1.0}]}}
    members_resp = {"data": {"diff": [
        {"f12": "600519", "f14": "茅台", "f3": 2.0, "f6": 1e9},
        {"f12": "INVALID", "f14": "无效", "f3": 0, "f6": 0},
        {"f12": "123", "f14": "短码", "f3": 0, "f6": 0},
    ]}}
    responses = [_mock_response(sector_resp), _mock_response(sector_resp),
                 _mock_response(members_resp)]
    with patch("urllib.request.urlopen", side_effect=responses):
        result = daily_scan.scan_hotspot_stocks(top_sectors=1, top_stocks_per_sector=3)
    codes = [r["code"] for r in result]
    assert "600519" in codes
    assert "INVALID" not in codes
    assert "123" not in codes


def test_scan_hotspot_default_params():
    """默认参数:top_sectors=5, top_stocks_per_sector=5。"""
    import inspect
    sig = inspect.signature(daily_scan.scan_hotspot_stocks)
    assert sig.parameters["top_sectors"].default == 5
    assert sig.parameters["top_stocks_per_sector"].default == 5
