"""到价提醒单元测试。不联网(实时价接口 mock)。"""


import alerts
import bot_handlers
from bot_context import Ctx


def _mock_rt(monkeypatch, price_map):
    """mock fetch_realtime: alerts 与 bot_handlers(strategy_engine) 两处引用。"""
    import strategy_engine

    def fake(codes):
        return [
            {"code": c, "name": f"N{c}", "price": price_map[c], "pct": 0.0}
            for c in codes if c in price_map
        ]

    # alerts.check_alerts_once 内部是 from data_fetcher import(运行时解析)
    import data_fetcher
    monkeypatch.setattr(data_fetcher, "fetch_realtime", fake)
    # handler 内部 import strategy_engine 后调 fetch_realtime(模块级引用)
    monkeypatch.setattr(strategy_engine, "fetch_realtime", fake)


def test_alert_add_list_remove(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ALERTS_DB", tmp_path / "a.db")
    assert "已设置" in alerts.alert_add("s1", "600519", "贵州茅台", "below", 1700)
    assert "已设置" in alerts.alert_add("s1", "600519", "贵州茅台", "above", 1800)
    # 重复提醒被拒绝
    assert "已存在" in alerts.alert_add("s1", "600519", "贵州茅台", "below", 1700)
    # 不同会话隔离
    assert alerts.alert_list("s1") != []
    assert alerts.alert_list("s2") == []
    # 删除(above + below 两条)
    assert "2 条" in alerts.alert_remove("s1", "600519")
    assert alerts.alert_list("s1") == []


def test_alert_check_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ALERTS_DB", tmp_path / "a.db")
    _mock_rt(monkeypatch, {"600519": 1690.0})
    alerts.alert_add("oc_x:user1", "600519", "贵州茅台", "below", 1700)

    sent = []
    import feishu

    class FakeBot:
        enabled = True

        def send_text(self, text, chat_id=None):
            sent.append((chat_id, text))
            return {"code": 0}

    monkeypatch.setattr(feishu, "FeishuBot", FakeBot)

    msgs = alerts.check_alerts_once()
    assert len(msgs) == 1
    assert sent[0][0] == "oc_x"  # 推送到 chat_id
    assert "跌破" in msgs[0]
    # 触发后变 inactive,不会重复触发
    assert alerts.check_alerts_once() == []
    assert sent.__len__() == 1


def test_alert_check_above_op(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ALERTS_DB", tmp_path / "a.db")
    _mock_rt(monkeypatch, {"600519": 1810.0})
    alerts.alert_add("s1", "600519", "贵州茅台", "above", 1800)
    assert alerts.check_alerts_once(dry_run=True)  # dry-run 不落库
    # dry-run 后仍是 active
    assert len(alerts.check_alerts_once(dry_run=True)) == 1


def test_alert_market_time():
    from datetime import datetime

    assert alerts.is_market_time(datetime(2026, 9, 10, 10, 0))
    assert alerts.is_market_time(datetime(2026, 9, 10, 13, 30))
    assert not alerts.is_market_time(datetime(2026, 9, 10, 12, 0))  # 午休
    assert not alerts.is_market_time(datetime(2026, 9, 10, 15, 30))
    assert not alerts.is_market_time(datetime(2026, 9, 10, 9, 0))


def test_handler_alerts_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts, "ALERTS_DB", tmp_path / "a.db")
    _mock_rt(monkeypatch, {"600519": 1690.0})
    monkeypatch.setattr(
        "stock_names.resolve_code", lambda q: "600519" if "茅台" in q or "600519" in q else None
    )
    ctx = Ctx(session_id="oc_x:user1")

    # add: 未指定 op,现价 1690 < 1700,应推断为 above
    r = bot_handlers.TOOL_HANDLERS["manage_alerts"](
        ctx, {"action": "add", "code": "茅台", "price": 1700}
    )
    assert "已设置" in r and "涨到" in r

    # list
    r = bot_handlers.TOOL_HANDLERS["manage_alerts"](ctx, {"action": "list"})
    assert "茅台" in r and "1 条" in r

    # remove
    r = bot_handlers.TOOL_HANDLERS["manage_alerts"](
        ctx, {"action": "remove", "code": "600519"}
    )
    assert "已删除" in r

    r = bot_handlers.TOOL_HANDLERS["manage_alerts"](ctx, {"action": "list"})
    assert "没有设置" in r
