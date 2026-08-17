"""API 接口集成测试（需要本地服务运行于 18000，否则 skip）"""

import json
import os
import urllib.request

import pytest

BASE = os.environ.get("QUANT_API_BASE", "http://127.0.0.1:18000")


def _get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return r.status, json.loads(r.read())


@pytest.fixture(scope="module")
def api_up():
    try:
        status, _ = _get("/api/watchlist")
        return status == 200
    except Exception:
        return False


@pytest.mark.skipif(not os.environ.get("RUN_API_TESTS"), reason="仅当 RUN_API_TESTS=1 且本地服务运行时执行")
def test_watchlist(api_up):
    assert api_up, "本地服务未运行"
    status, data = _get("/api/watchlist")
    assert status == 200
    keys = {r["code"] for r in data.get("data", [])}
    assert "600789" in keys or bool(keys)


@pytest.mark.skipif(not os.environ.get("RUN_API_TESTS"), reason="仅当 RUN_API_TESTS=1 时执行")
def test_root_ok():
    with urllib.request.urlopen(f"{BASE}/", timeout=10) as r:
        assert r.status == 200
