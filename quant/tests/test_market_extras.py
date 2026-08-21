"""stock_market_extras 5 个新数据接口单元测试(不联网)。

覆盖:
  - fetch_lhb: 龙虎榜
  - fetch_north_flow: 北向资金
  - fetch_main_flow: 主力资金流
  - fetch_concept_sectors: 概念板块反查
  - fetch_index: 指数行情
  - 格式化函数(fmt_*)
"""

import json
from unittest.mock import MagicMock, patch

import stock_market_extras as sme


def _mock_resp(data):
    """构造 MagicMock 模拟 HTTP 响应(jsonp/json)。"""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return resp


# ---------------- 龙虎榜 ----------------


def test_fetch_lhb_invalid_response():
    """接口失败时应返回空列表。"""
    with patch("urllib.request.urlopen", side_effect=Exception("net error")):
        result = sme.fetch_lhb()
    assert result == []


def test_fetch_lhb_parses_response():
    """正常 JSON 响应应解析为龙虎榜列表。"""
    jsonp_data = {
        "success": True,
        "result": {
            "data": [
                {
                    "TRADE_DATE": "2026-08-20 00:00:00",
                    "SECURITY_CODE": "002365",
                    "SECURITY_NAME_ABBR": "永安药业",
                    "CLOSE_PRICE": 15.76,
                    "CHANGE_RATE": 9.98,
                    "BILLBOARD_BUY_AMT": 162419348,
                    "BILLBOARD_SELL_AMT": 112842809,
                    "BILLBOARD_NET_AMT": 49576539,
                    "EXPLAIN": "主力做T",
                    "EXPLANATION": "日换手率达到20%",
                    "DEAL_AMOUNT_RATIO": 24.1,
                },
                # 同一股票第二个原因(应去重合并)
                {
                    "TRADE_DATE": "2026-08-20 00:00:00",
                    "SECURITY_CODE": "002365",
                    "SECURITY_NAME_ABBR": "永安药业",
                    "CLOSE_PRICE": 15.76,
                    "CHANGE_RATE": 9.98,
                    "BILLBOARD_BUY_AMT": 162419348,
                    "BILLBOARD_SELL_AMT": 112842809,
                    "BILLBOARD_NET_AMT": 49576539,
                    "EXPLAIN": "主力做T",
                    "EXPLANATION": "日涨幅偏离值达到7%",
                    "DEAL_AMOUNT_RATIO": 24.1,
                },
            ]
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_lhb()
    assert len(result) == 1  # 去重后 1 条
    r = result[0]
    assert r["code"] == "002365"
    assert r["name"] == "永安药业"
    assert "日换手率达到20%" in r["reason"]
    assert "日涨幅偏离值达到7%" in r["reason"]
    assert r["date"] == "2026-08-20"
    assert r["close"] == 15.76


def test_fetch_lhb_with_date_filter():
    """date_str 参数应添加日期过滤到 URL。"""
    captured_url = []

    def fake_urlopen(req, timeout=10):
        captured_url.append(req.full_url)
        return _mock_resp({"success": True, "result": {"data": []}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        sme.fetch_lhb(date_str="20260820")
    assert any("2026-08-20" in u for u in captured_url)


def test_fmt_lhb_empty():
    """空列表应有友好提示。"""
    assert "无龙虎榜数据" in sme.fmt_lhb([])


def test_fmt_lhb_formats():
    """应正确格式化龙虎榜。"""
    rows = [{
        "code": "002365", "name": "永安药业", "date": "2026-08-20",
        "close": 15.76, "pct": 9.98, "net_amt": 49576539,
        "reason": "日换手率20%", "explain": "主力做T",
    }]
    text = sme.fmt_lhb(rows)
    assert "永安药业" in text
    assert "002365" in text
    assert "龙虎榜" in text


# ---------------- 北向资金 ----------------


def test_fetch_north_flow_invalid_response():
    """接口失败应返回空。"""
    with patch("urllib.request.urlopen", side_effect=Exception("net error")):
        result = sme.fetch_north_flow()
    assert result == []


def test_fetch_north_flow_parses():
    """应解析 hk2sh / hk2sz / sh2hk 字段。"""
    jsonp_data = {
        "data": {
            # 余额=0,额度=520亿 -> 净流入520亿(单位:元)
            # 实际东财 hk2sh 单位是百万元,所以 5200000 = 520 万 = 5.2 亿
            # 但接口实测 hk2sh[1] 是余额, [2]是额度, 差值就是净流入
            # 我们用大数字让差值清晰: 余额 1亿, 额度 50亿 -> 净流入 49亿
            "hk2sh": ["2026-08-21,100000000.00,5000000000.00,0.00"],
            "hk2sz": ["2026-08-21,200000000.00,5000000000.00,0.00"],
            "sh2hk": ["2026-08-21,5000000000.00,5000000000.00,0.00"],
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_north_flow(days=1)
    assert len(result) == 1
    r = result[0]
    assert r["date"] == "2026-08-21"
    # 净流入 = (额度 - 余额) / 1e8
    assert r["hk2sh_net"] == round((5000000000 - 100000000) / 1e8, 2)  # 49.0 亿
    assert r["hk2sz_net"] == round((5000000000 - 200000000) / 1e8, 2)  # 48.0 亿
    assert r["total_net"] == 97.0


def test_fmt_north_flow():
    """格式化北向资金。"""
    rows = [{"date": "2026-08-21", "hk2sh_net": 5.5, "hk2sz_net": 3.3, "total_net": 8.8}]
    text = sme.fmt_north_flow(rows)
    assert "北向" in text
    assert "5.50" in text or "5.5" in text


def test_fmt_north_flow_empty():
    assert "无北向资金数据" in sme.fmt_north_flow([])


# ---------------- 主力资金流 ----------------


def test_fetch_main_flow_invalid_code():
    """非法代码应返回 error。"""
    assert "error" in sme.fetch_main_flow("")
    assert "error" in sme.fetch_main_flow("12345")
    assert "error" in sme.fetch_main_flow("abcdef")


def test_fetch_main_flow_network_error():
    """网络异常应返回 error。"""
    with patch("urllib.request.urlopen", side_effect=Exception("net error")):
        result = sme.fetch_main_flow("600519")
    assert "error" in result


def test_fetch_main_flow_parses():
    """正常响应应解析,主力净流入 = 超大单 + 大单。"""
    jsonp_data = {
        "data": {
            "f57": "600519", "f58": "贵州茅台",
            "f43": 12731500,  # 价格需 /100 -> 1273.15?但 600519 茅台 1000+,>1000 才 /100
            "f184": 1.30,    # 主力占比%
            "f135": 1628417040,  # 超大单 16.28 亿
            "f136": 2158511184,  # 大单 21.59 亿
            "f137": -551485584,  # 中单 -5.51 亿
            "f138": 777188960,   # 小单 7.77 亿
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_main_flow("600519")
    assert "error" not in result
    assert result["code"] == "600519"
    assert result["name"] == "贵州茅台"
    assert result["super_large"] == 16.28
    assert result["large"] == 21.59
    assert result["main_net"] == round(16.28 + 21.59, 2)  # 37.87
    assert result["main_pct"] == 1.30


def test_fmt_main_flow_error():
    """error dict 应格式化为 ❌。"""
    text = sme.fmt_main_flow({"error": "失败"})
    assert "❌" in text
    assert "失败" in text


def test_fmt_main_flow_formats():
    """正常 dict 应格式化为资金流文本。"""
    d = {"name": "茅台", "code": "600519", "main_net": 5.5, "main_pct": 1.3,
         "super_large": 3.0, "large": 2.5, "medium": -1.5, "small": 1.0}
    text = sme.fmt_main_flow(d)
    assert "茅台" in text
    assert "主力净流入" in text
    assert "5.50" in text or "5.5" in text


# ---------------- 概念板块反查 ----------------


def test_fetch_concept_sectors_invalid_code():
    """非法代码应返回空。"""
    assert sme.fetch_concept_sectors("") == []
    assert sme.fetch_concept_sectors("12345") == []


def test_fetch_concept_sectors_network_error():
    """网络异常应返回空。"""
    with patch("urllib.request.urlopen", side_effect=Exception("net error")):
        result = sme.fetch_concept_sectors("600519")
    assert result == []


def test_fetch_concept_sectors_parses():
    """应解析板块列表并去重(同 BOARD_CODE)。"""
    jsonp_data = {
        "success": True,
        "result": {
            "data": [
                {"BOARD_NAME": "白酒Ⅱ", "BOARD_CODE": "BK1277"},
                {"BOARD_NAME": "白酒Ⅱ", "BOARD_CODE": "BK1277"},  # 重复,应去重
                {"BOARD_NAME": "酿酒行业", "BOARD_CODE": "BK0477"},
            ]
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_concept_sectors("600519")
    assert len(result) == 2  # 去重后 2 个
    assert result[0]["board_code"] == "BK1277"
    assert result[1]["board_code"] == "BK0477"


def test_fmt_concept_sectors_empty():
    assert "无板块数据" in sme.fmt_concept_sectors([])


def test_fmt_concept_sectors_formats():
    sectors = [{"board_name": "白酒Ⅱ", "board_code": "BK1277", "board_type": "industry"}]
    text = sme.fmt_concept_sectors(sectors)
    assert "白酒Ⅱ" in text
    assert "BK1277" in text


# ---------------- 指数行情 ----------------


def test_fetch_index_unknown_name():
    """未知指数名应返回 error。"""
    result = sme.fetch_index("不存在的指数")
    assert "error" in result


def test_fetch_index_known_name():
    """已知指数名应返回行情(通过 mock)。"""
    jsonp_data = {
        "data": {
            "f57": "000001", "f58": "上证指数",
            "f43": 389674,  # 需 /100 -> 3896.74
            "f170": -18,    # -0.18%
            "f169": -700,   # -7.00
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_index("上证")
    assert "error" not in result
    # INDEX_MAP["上证"] 优先匹配,返回的 name 是 "上证"
    assert "上证" in result["name"]
    assert result["price"] == 3896.74
    assert result["pct"] == -0.18
    assert result["change"] == -7.0


def test_fetch_index_all():
    """不传 name 应返回主要指数列表。"""
    jsonp_data = {
        "data": {
            "f57": "000001", "f58": "上证",
            "f43": 389674, "f170": -18, "f169": -700,
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_index()  # 全部
    assert isinstance(result, list)
    assert len(result) >= 1
    assert result[0]["name"] == "上证指数"


def test_fetch_index_fuzzy_match():
    """模糊匹配指数名(如 '上证指' 应匹配 '上证指数')。"""
    jsonp_data = {"data": {"f57": "000001", "f58": "上证", "f43": 389674, "f170": -18, "f169": -700}}
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_index("上证指")  # 模糊匹配
    assert "error" not in result


def test_fmt_index_single():
    """单指数格式化。"""
    d = {"name": "上证", "code": "000001", "price": 3896.74, "pct": -0.18, "change": -7.0}
    text = sme.fmt_index(d)
    assert "上证" in text
    assert "3896.74" in text


def test_fmt_index_list():
    """指数列表格式化。"""
    data = [{"name": "上证", "code": "000001", "price": 3896, "pct": -0.18, "change": -7}]
    text = sme.fmt_index(data)
    assert "上证" in text


def test_fmt_index_error():
    """error 格式化。"""
    text = sme.fmt_index({"error": "失败"})
    assert "❌" in text


# ---------------- 飞书 Bot handler 注册 ----------------


def test_handlers_registered_in_tool_handlers():
    """5 个新 skill 应在 TOOL_HANDLERS 中注册。"""
    import feishu_bot
    for sid in ("get_lhb", "get_north_flow", "get_main_flow",
                "get_concept_sectors", "get_index"):
        assert sid in feishu_bot.TOOL_HANDLERS, f"{sid} 未注册到 TOOL_HANDLERS"


def test_handlers_registered_in_tools():
    """5 个新 skill 应在 TOOLS 列表中。"""
    import feishu_bot
    tool_names = {t["function"]["name"] for t in feishu_bot.TOOLS}
    for sid in ("get_lhb", "get_north_flow", "get_main_flow",
                "get_concept_sectors", "get_index", "scan_with_yujie"):
        assert sid in tool_names, f"{sid} 未在 TOOLS 列表中"


def test_scan_with_yujie_in_slow_tools():
    """scan_with_yujie 应在 SLOW_TOOLS 中(耗时 1-3 分钟)。"""
    import feishu_bot
    assert "scan_with_yujie" in feishu_bot.SLOW_TOOLS


def test_tool_count_27():
    """工具总数应为 27(21 旧 + 5 市场 + 1 玉姐扫描)。"""
    import feishu_bot
    assert len(feishu_bot.TOOLS) == 27
    assert len(feishu_bot.TOOL_HANDLERS) == 27
