"""飞书 Bot 推送单元测试。mock urllib,不联网。"""

import json
import time
from unittest.mock import patch

import feishu


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _mock_config(tmp_path, monkeypatch, cfg):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"feishu": cfg}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(feishu, "CONFIG_PATH", cfg_path)


def test_load_feishu_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu, "CONFIG_PATH", tmp_path / "no.json")
    assert feishu._load_feishu_config() == {}


def test_load_feishu_config_ok(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "cli_x", "app_secret": "sec", "chat_id": "oc_y"
    })
    cfg = feishu._load_feishu_config()
    assert cfg["app_id"] == "cli_x"
    assert cfg["enabled"] is True


def test_bot_disabled_skips_send(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {"enabled": False})
    bot = feishu.FeishuBot()
    assert bot.send_text("x") is None


def test_bot_no_chat_id_skips_send(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": ""
    })
    bot = feishu.FeishuBot()
    assert bot.send_text("x") is None


def test_get_token_caches(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })
    bot = feishu.FeishuBot()
    calls = {"n": 0}

    def fake_post(url, body, bearer=None, timeout=10):
        calls["n"] += 1
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "tok1", "expire": 7200}
        return {"code": 0, "data": {"message_id": "m1"}}

    with patch("feishu._post_json", side_effect=fake_post):
        t1 = bot._get_token()
        t2 = bot._get_token()  # 应命中缓存
    assert t1 == "tok1" and t2 == "tok1"
    assert calls["n"] == 1  # token 接口只调一次


def test_send_card_success(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })
    bot = feishu.FeishuBot()
    calls = []

    def fake_post(url, body, bearer=None, timeout=10):
        calls.append((url, body, bearer))
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "tok", "expire": 7200}
        return {"code": 0, "data": {"message_id": "m1"}}

    with patch("feishu._post_json", side_effect=fake_post):
        resp = bot.send_card({"header": {"title": {"content": "test"}}})
    assert resp["code"] == 0
    # 第二次调用应是发消息接口,带 Bearer
    assert "im/v1/messages" in calls[1][0]
    assert calls[1][2] == "tok"


def test_send_text_failure_returns_resp(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })
    bot = feishu.FeishuBot()

    def fake_post(url, body, bearer=None, timeout=10):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "tok", "expire": 7200}
        return {"code": 230002, "msg": "token expired"}

    with patch("feishu._post_json", side_effect=fake_post):
        resp = bot.send_text("hello")
    assert resp["code"] == 230002


def test_send_text_network_exception_returns_none(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })
    bot = feishu.FeishuBot()

    def fake_post(url, body, bearer=None, timeout=10):
        raise OSError("network down")

    with patch("feishu._post_json", side_effect=fake_post):
        resp = bot.send_text("hello")
    assert resp is None


# -------- 卡片构造 --------


def test_build_daily_card_bullish():
    stats = {
        "total": 5000, "up": 3500, "down": 1000, "flat": 500,
        "limit_up": 80, "limit_down": 5, "total_amount_yi": 12000,
    }
    cands = [
        {"rank": 1, "code": "000001", "name": "平安银行", "score": 8,
         "verdict": "买入", "hits": ["MACD金叉", "放量"]},
    ]
    card = feishu.build_daily_card(stats, cands, "AI 综合分析内容...")
    assert card["header"]["template"] == "green"  # 偏多
    assert "5000" in card["elements"][0]["text"]["content"]
    assert "平安银行" in card["elements"][3]["text"]["content"]
    assert "AI 综合分析内容..." in card["elements"][6]["text"]["content"]


def test_build_daily_card_bearish():
    stats = {"total": 5000, "up": 1000, "down": 3500, "flat": 500,
             "limit_up": 5, "limit_down": 80, "total_amount_yi": 8000}
    card = feishu.build_daily_card(stats, [], "")
    assert card["header"]["template"] == "red"
    assert "（无候选）" in card["elements"][3]["text"]["content"]
    assert "(AI 调用失败)" in card["elements"][6]["text"]["content"]


def test_build_daily_card_neutral():
    stats = {"total": 5000, "up": 2000, "down": 2000, "flat": 1000,
             "limit_up": 30, "limit_down": 30, "total_amount_yi": 10000}
    card = feishu.build_daily_card(stats, [], "")
    assert card["header"]["template"] == "blue"


def test_build_daily_card_truncates_long_ai():
    stats = {"total": 0, "up": 0, "down": 0, "flat": 0,
             "limit_up": 0, "limit_down": 0, "total_amount_yi": 0}
    long_text = "x" * 2000
    card = feishu.build_daily_card(stats, [], long_text)
    body = card["elements"][6]["text"]["content"]
    assert len(body) < 1000
    assert body.endswith("...")


# -------- 主流程接入 --------


def test_send_daily_to_feishu_disabled(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {"enabled": False})
    stats = {"total": 1, "up": 1, "down": 0, "flat": 0,
             "limit_up": 0, "limit_down": 0, "total_amount_yi": 1}
    assert feishu.send_daily_to_feishu(stats, [], "x") is False


def test_send_daily_to_feishu_success(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })

    def fake_post(url, body, bearer=None, timeout=10):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "tok", "expire": 7200}
        return {"code": 0, "data": {"message_id": "m1"}}

    with patch("feishu._post_json", side_effect=fake_post):
        stats = {"total": 1, "up": 1, "down": 0, "flat": 0,
                 "limit_up": 0, "limit_down": 0, "total_amount_yi": 1}
        ok = feishu.send_daily_to_feishu(stats, [], "AI 摘要")
    assert ok is True


def test_send_daily_to_feishu_exception_safe(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })

    def fake_post(url, body, bearer=None, timeout=10):
        raise OSError("net error")

    with patch("feishu._post_json", side_effect=fake_post):
        # 不应抛异常
        ok = feishu.send_daily_to_feishu({"total": 0}, [], "x")
    assert ok is False


def test_token_expiry_refreshes(tmp_path, monkeypatch):
    _mock_config(tmp_path, monkeypatch, {
        "enabled": True, "app_id": "x", "app_secret": "y", "chat_id": "oc_z"
    })
    bot = feishu.FeishuBot()
    calls = {"n": 0}

    def fake_post(url, body, bearer=None, timeout=10):
        calls["n"] += 1
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": f"tok{calls['n']}", "expire": 7200}
        return {"code": 0}

    with patch("feishu._post_json", side_effect=fake_post):
        bot._get_token()
        # 模拟过期
        bot._token_expire_at = time.time() - 1
        bot._get_token()
    assert calls["n"] == 2  # 第二次重新获取
