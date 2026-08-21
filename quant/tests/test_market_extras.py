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


def test_fetch_index_full_fields():
    """fetch_index 应返回扩展字段(开高低昨收/成交额/振幅/量比/换手)。"""
    jsonp_data = {
        "data": {
            "f57": "000001", "f58": "上证指数",
            "f43": 389674,   # 3896.74
            "f44": 391213,   # high
            "f45": 388379,   # low
            "f46": 389118,   # open
            "f60": 390372,   # pre_close
            "f48": 883423480098.6,  # amount(元)
            "f50": 87,       # 量比 0.87
            "f168": 92,      # 换手 0.92%
            "f169": 148,     # change
            "f170": 4,       # pct 0.04%
            "f171": 73,      # 振幅 0.73%
        }
    }
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp_data)):
        result = sme.fetch_index("上证")
    assert "error" not in result
    assert result["open"] == 3891.18
    assert result["high"] == 3912.13
    assert result["low"] == 3883.79
    assert result["pre_close"] == 3903.72
    assert result["amount"] == 883423480098.6
    assert result["amplitude"] == 0.73
    assert result["qr"] == 0.87
    assert result["turnover"] == 0.92


def test_fmt_index_full_fields():
    """完整字段格式化应包含成交额/振幅/量比/换手。"""
    d = {
        "name": "上证指数", "code": "000001", "price": 3896.74,
        "pct": 0.04, "change": 1.48,
        "open": 3891.18, "high": 3912.13, "low": 3883.79, "pre_close": 3903.72,
        "amount": 883423480098.6, "amplitude": 0.73, "qr": 0.87, "turnover": 0.92,
    }
    text = sme.fmt_index(d)
    assert "3896.74" in text
    assert "今开 3891.18" in text
    assert "高 3912.13 / 低 3883.79" in text
    assert "昨收 3903.72" in text
    assert "成交额 8834亿" in text
    assert "振幅 0.73%" in text
    assert "量比 0.87" in text
    assert "换手 0.92%" in text


def test_fmt_index_amount_format():
    """成交额格式化:亿为单位,千以下保留 1 位小数。"""
    d = {"name": "上证", "code": "000001", "price": 100, "pct": 0.5, "change": 0.5,
         "amount": 12345678900.0}  # 123.46 亿
    text = sme.fmt_index(d)
    assert "成交额 123亿" in text


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
                "get_concept_sectors", "get_index", "scan_with_yujie",
                "get_sector_flow", "get_market_sentiment"):
        assert sid in tool_names, f"{sid} 未在 TOOLS 列表中"


def test_scan_with_yujie_in_slow_tools():
    """scan_with_yujie 应在 SLOW_TOOLS 中(耗时 1-3 分钟)。"""
    import feishu_bot
    assert "scan_with_yujie" in feishu_bot.SLOW_TOOLS


def test_tool_count_30():
    """工具总数应为 30(29 旧 + 1 组合回测)。"""
    import feishu_bot
    assert len(feishu_bot.TOOLS) == 30
    assert len(feishu_bot.TOOL_HANDLERS) == 30


# ---------------- 板块资金流 / 市场情绪 ----------------


def test_fetch_sector_flow_industry(monkeypatch):
    """行业板块资金流: mock push2 clist 返回 2 个板块。"""
    jsonp = {"data": {"diff": [
        {"f12": "BK0478", "f14": "有色金属", "f2": 4123.45, "f3": 2.59,
         "f62": 8828891648, "f184": 6.09, "f66": 5e9, "f72": 3e9, "f75": -1e9, "f78": -7e9},
        {"f12": "BK0732", "f14": "电子", "f2": 3567.89, "f3": 0.73,
         "f62": 8552960000, "f184": 1.75, "f66": 4e9, "f72": 2e9, "f75": -1e9, "f78": -5e9},
    ]}}
    with patch("urllib.request.urlopen", return_value=_mock_resp(jsonp)):
        rows = sme.fetch_sector_flow("industry", top_n=10)
    assert len(rows) == 2
    assert rows[0]["name"] == "有色金属"
    assert rows[0]["pct"] == 2.59
    assert rows[0]["main_net"] == 8828891648
    assert rows[0]["super_large_net"] == 5e9


def test_fetch_sector_flow_unknown_type():
    """未知板块类型应返 error。"""
    rows = sme.fetch_sector_flow("unknown")
    assert isinstance(rows, list)
    assert "error" in rows[0]


def test_fmt_sector_flow_basic():
    """板块资金流格式化。"""
    rows = [
        {"name": "有色金属", "pct": 2.59, "main_net": 8828891648, "main_pct": 6.09},
        {"name": "电子", "pct": 0.73, "main_net": 8552960000, "main_pct": 1.75},
    ]
    text = sme.fmt_sector_flow(rows, "行业板块")
    assert "行业板块" in text
    assert "有色金属" in text
    assert "+88.29亿" in text  # 8828891648 / 1e8 = 88.29
    assert "2.59%" in text


def test_fmt_sector_flow_empty():
    """空数据格式化。"""
    text = sme.fmt_sector_flow([], "板块")
    assert "无" in text


def test_fetch_market_sentiment(monkeypatch):
    """市场情绪: mock fetch_index + fetch_sector_flow。"""
    # mock fetch_index 返回 2 个指数
    fake_idx = [
        {"name": "上证指数", "code": "000001", "price": 3905.2, "pct": 0.04, "change": 1.48,
         "amount": 883423480098.6},
        {"name": "深证成指", "code": "399001", "price": 14094.17, "pct": 0.87, "change": 121.39,
         "amount": 995840925246.3},
    ]
    fake_industry = [{"name": "有色", "pct": 2.59, "main_net": 8.8e9}]
    fake_concept = [{"name": "5G", "pct": 1.13, "main_net": 1.4e10}]
    monkeypatch.setattr(sme, "fetch_index", lambda: fake_idx)
    monkeypatch.setattr(sme, "fetch_sector_flow", lambda st, top_n=10:
                        fake_industry if st == "industry" else fake_concept)
    data = sme.fetch_market_sentiment()
    assert len(data["indices"]) == 2
    assert data["indices"][0]["name"] == "上证指数"
    assert data["indices"][0]["amount_yi"] == 8834  # 883423480098.6 / 1e8 = 8834
    assert data["top_industries"] == fake_industry
    assert data["top_concepts"] == fake_concept


def test_fmt_market_sentiment_basic():
    """市场情绪格式化:含指数+行业+概念。"""
    data = {
        "indices": [
            {"name": "上证指数", "pct": 0.04, "amount_yi": 8834},
            {"name": "创业板指", "pct": 1.43, "amount_yi": 4945},
        ],
        "top_industries": [{"name": "有色", "pct": 2.59, "main_net": 8.8e9}],
        "top_concepts": [{"name": "5G", "pct": 1.13, "main_net": 1.4e10}],
    }
    text = sme.fmt_market_sentiment(data)
    assert "市场情绪" in text
    assert "上证指数" in text
    assert "+0.04%" in text
    assert "8834亿" in text
    assert "有色" in text
    assert "5G" in text
    assert "+88.00亿" in text  # 8.8e9 / 1e8 = 88


def test_handler_get_sector_flow_registered():
    """handler_get_sector_flow 应在 TOOL_HANDLERS 注册。"""
    import feishu_bot
    assert "get_sector_flow" in feishu_bot.TOOL_HANDLERS


def test_handler_get_market_sentiment_registered():
    """handler_get_market_sentiment 应在 TOOL_HANDLERS 注册。"""
    import feishu_bot
    assert "get_market_sentiment" in feishu_bot.TOOL_HANDLERS


# ---------------- combo_backtest 组合回测 ----------------


def test_combo_backtest_registered():
    """combo_backtest 应在 TOOLS + HANDLERS + SLOW_TOOLS 注册。"""
    import feishu_bot
    tool_names = {t["function"]["name"] for t in feishu_bot.TOOLS}
    assert "combo_backtest" in tool_names
    assert "combo_backtest" in feishu_bot.TOOL_HANDLERS
    assert "combo_backtest" in feishu_bot.SLOW_TOOLS


def test_run_combo_backtest_or_mode(monkeypatch):
    """OR 模式: 任一策略触发即记组合信号。"""
    import numpy as np
    import pandas as pd

    import backtest_builtin as bb

    # mock _get_universe_codes 返回 2 只
    monkeypatch.setattr(bb, "_get_universe_codes", lambda: ["600519", "000001"])

    # mock _load_code 返回合成 df
    def fake_load(code):
        n = 250
        rng = np.random.default_rng(hash(code) % 2**32)
        close = 10 + np.cumsum(rng.normal(0, 0.1, n))
        return pd.DataFrame({
            "date": [f"20260{i:04d}" for i in range(n)],
            "open": close, "close": close,
            "high": close * 1.02, "low": close * 0.98,
            "volume": [1e6] * n,
        })
    monkeypatch.setattr(bb, "_load_code", fake_load)

    r = bb.run_combo_backtest(["macd", "boll"], mode="or", horizon=20, sample=0, workers=1)
    assert "error" not in r
    assert r["mode"] == "or"
    assert r["combo"]["signal_count"] >= 0
    assert "macd" in r["per_strategy"]
    assert "boll" in r["per_strategy"]


def test_run_combo_backtest_unknown_strategy():
    """未知策略应返 error。"""
    import backtest_builtin as bb
    r = bb.run_combo_backtest(["unknown_strat", "macd"], mode="and")
    assert "error" in r


def test_run_combo_backtest_single_strategy():
    """少于 2 个策略应返 error。"""
    import backtest_builtin as bb
    r = bb.run_combo_backtest(["macd"], mode="and")
    assert "error" in r


def test_run_combo_backtest_invalid_mode():
    """无效 mode 应返 error。"""
    import backtest_builtin as bb
    r = bb.run_combo_backtest(["macd", "boll"], mode="invalid")
    assert "error" in r
