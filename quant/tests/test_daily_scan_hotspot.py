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


# ---------------- 盘后复盘测试 ----------------


class TestAfterClose:
    """run_after_close / build_afterclose_prompt / 卡片构造测试(mock,不联网)。"""

    def test_build_afterclose_prompt(self):
        import daily_scan

        stats = {"total": 5000, "up": 2500, "down": 2000, "flat": 500,
                 "limit_up": 40, "limit_down": 10, "total_amount_yi": 15000.0}
        picks = [{"code": "600519", "name": "贵州茅台", "score": 8, "hits": ["多头", "放量"], "rank": 1, "pct": 2.5}]
        sectors = ["行业板块Top5: 白酒 | 银行"]
        out = daily_scan.build_afterclose_prompt(stats, picks, sectors, [], "2026-09-01")
        assert "收盘全景" in out
        assert "玉姐" in out
        assert "+2.50%" in out
        assert "复盘" in out

    def test_build_afterclose_prompt_empty(self):
        import daily_scan

        stats = {"total": 0, "up": 0, "down": 0, "flat": 0, "limit_up": 0,
                 "limit_down": 0, "total_amount_yi": 0.0}
        out = daily_scan.build_afterclose_prompt(stats, [], ["(无)"], [], "2026-09-01")
        assert "今日无早盘推荐" in out

    def test_build_afterclose_card(self):
        import feishu

        stats = {"total": 5000, "up": 2500, "down": 2000, "flat": 500,
                 "limit_up": 40, "limit_down": 10, "total_amount_yi": 15000.0}
        picks = [{"code": "600519", "name": "贵州茅台", "score": 8, "hits": ["多头"],
                  "rank": 1, "pct": 2.5}]
        card = feishu.build_afterclose_card(stats, picks, ["行业Top5: 白酒"], "AI 总结" * 500)
        assert card["header"]["title"]["content"].startswith("📊 A股盘后复盘")
        # AI 摘要超长截断
        ai_div = [e for e in card["elements"] if isinstance(e.get("text"), dict)]
        assert any("..." in d["text"]["content"] for d in ai_div if len(d["text"]["content"]) > 800)
        assert any("贵州茅台" in d["text"]["content"] for d in ai_div)

    def test_save_afterclose_report(self, tmp_path):
        import daily_scan

        stats = {"total": 5000, "up": 2500, "down": 2000, "flat": 500,
                 "limit_up": 40, "limit_down": 10, "total_amount_yi": 15000.0}
        picks = [{"code": "600519", "name": "贵州茅台", "score": 8, "hits": ["多头"],
                  "rank": 1, "pct": 2.5}]
        old = daily_scan.REPORTS
        daily_scan.REPORTS = tmp_path
        try:
            from datetime import datetime

            fp = daily_scan.save_afterclose_report(
                stats, picks, ["行业Top5: 白酒"], [], "AI 复盘正文", datetime.now()
            )
            content = fp.read_text(encoding="utf-8")
        finally:
            daily_scan.REPORTS = old
        assert fp.exists()
        assert "盘后复盘日报" in content
        assert "+2.50%" in content
        assert "AI 复盘正文" in content
