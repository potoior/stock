"""AI 网关故障切换测试:主网关 5xx/网络异常时自动切备用网关。"""

import ai_decider


class Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def make_decider_with(monkeypatch, call_results):
    """构造带 mock httpx 的 decider,call_results 为每次 POST 的响应(或异常)。"""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            calls.append(url)
            result = call_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr("httpx.Client", FakeClient)
    decider = ai_decider.AIDecider()
    return decider, calls


def test_failover_on_503(monkeypatch):
    """主网关 503 → 自动切备用网关。"""
    ok_body = {"choices": [{"message": {"content": "hello"}}]}
    monkeypatch.setenv("AI_FALLBACK_URL", "http://fallback.local/v1/chat/completions")
    monkeypatch.setenv("AI_FALLBACK_MODEL", "backup-model")
    decider, calls = make_decider_with(
        monkeypatch,
        [
            Resp(503),                                  # 主网关挂了
            Resp(200, ok_body),                         # 备用网关正常
        ],
    )
    out = decider._call_api("test")
    assert not out.startswith(("调用失败", "API错误", "API限流"))
    assert out.strip() == "hello"
    assert len(calls) == 2
    assert calls[1] == "http://fallback.local/v1/chat/completions"


def test_no_failover_on_200(monkeypatch):
    """主网关正常时不碰备用。"""
    ok_body = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setenv("AI_FALLBACK_URL", "http://fallback.local/v1/chat/completions")
    decider, calls = make_decider_with(monkeypatch, [Resp(200, ok_body)])
    out = decider._call_api("test")
    assert out.strip() == "ok"
    assert len(calls) == 1


def test_failover_on_network_error(monkeypatch):
    """网络异常也切换。"""
    ok_body = {"choices": [{"message": {"content": "ok"}}]}
    monkeypatch.setenv("AI_FALLBACK_URL", "http://fallback.local/v1/chat/completions")
    decider, _ = make_decider_with(
        monkeypatch,
        [
            ConnectionError("refused"),
            Resp(200, ok_body),
        ],
    )
    out = decider._call_api("test")
    assert out.strip() == "ok"


def test_all_fail_rate_limited(monkeypatch):
    """全部端点 503 → 返回 API限流 前缀(保留退避重试语义)。"""
    monkeypatch.setenv("AI_FALLBACK_URL", "http://fallback.local/v1/chat/completions")
    decider, _ = make_decider_with(monkeypatch, [Resp(503), Resp(503)])
    out = decider._call_api("test")
    assert out.startswith("API限流")


def test_no_fallback_configured(monkeypatch):
    """未配置备用网关时行为同旧版。"""
    monkeypatch.delenv("AI_FALLBACK_URL", raising=False)
    decider, calls = make_decider_with(monkeypatch, [Resp(503)])
    out = decider._call_api("test")
    assert out.startswith("API限流")
    assert len(calls) == 1


def test_4xx_no_failover(monkeypatch):
    """4xx 配置类错误不切换,直接返回(换网关也没用)。"""
    monkeypatch.setenv("AI_FALLBACK_URL", "http://fallback.local/v1/chat/completions")
    decider, calls = make_decider_with(monkeypatch, [Resp(401)])
    out = decider._call_api("test")
    assert out.startswith("API错误")
    assert len(calls) == 1
