"""sandbox_scan 单元测试:mock LLM/数据,不联网。"""


import pandas as pd

import sandbox_scan as ss


def _make_df(n=300, base=10.0):
    """合成上升行情的日 K 线。"""
    closes = [base * (1 + i * 0.001) for i in range(n)]
    return pd.DataFrame({
        "date": [f"202601{i % 28 + 1:02d}" for i in range(n)],
        "open": closes,
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": [1e8] * n,  # 保证过成交额过滤(price×volume ≈ 10亿)
    })


SAMPLES = {"600519": _make_df(), "000001": _make_df(), "300750": _make_df()}


class FakeDecider:
    """返回固定文本的假 LLM。"""

    def __init__(self, text):
        self.text = text

    def generate(self, prompt, timeout=90):
        return self.text


# ---------------- extract_code ----------------


def test_extract_code_fenced():
    raw = "说明\n```python\ndef strategy(df):\n    return True\n```\n完"
    assert "def strategy" in ss.extract_code(raw)


def test_extract_code_bare():
    raw = "def strategy(df):\n    return True"
    assert "def strategy" in ss.extract_code(raw)


def test_extract_code_garbage():
    assert ss.extract_code("没有代码") is None


# ---------------- run_strategy ----------------


def test_run_strategy_basic():
    code = "def strategy(df):\n    return df['close'].iloc[-1] > 0"
    assert ss.run_strategy(code, _make_df(10)) is True


def test_run_strategy_exception():
    code = "def strategy(df):\n    return df['不存在的列'].iloc[-1]"
    assert ss.run_strategy(code, _make_df(10)) is False


def test_run_strategy_non_bool():
    code = "def strategy(df):\n    return '字符串'"
    assert ss.run_strategy(code, _make_df(10)) is False


# ---------------- _validate_code ----------------


def test_validate_ok():
    code = "import pandas as pd\n\ndef strategy(df):\n    return df['close'].iloc[-1] > 0"
    ok, err = ss._validate_code(code, SAMPLES)
    assert ok, err


def test_validate_syntax_error():
    ok, err = ss._validate_code("def strategy(:", SAMPLES)
    assert not ok


def test_validate_missing_function():
    ok, _err = ss._validate_code("x = 1", SAMPLES)
    assert not ok


def test_validate_runtime_error():
    code = "def strategy(df):\n    return df['close'].iloc[99999]"
    ok, _err = ss._validate_code(code, SAMPLES)
    assert not ok


# ---------------- scan_custom 全流程 ----------------


def test_scan_custom_full_flow(monkeypatch, tmp_path):
    """LLM 生码 → 样本验证 → 沙箱扫描 → 命中。"""
    monkeypatch.setattr(ss, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(ss, "AIDecider", lambda: FakeDecider(
        "```python\ndef strategy(df):\n    return df['close'].iloc[-1] > 0\n```"
    ))
    monkeypatch.setattr(ss, "_load_samples", lambda: SAMPLES)
    monkeypatch.setattr(ss, "_load_candidates", lambda limit=0: ["600519", "000001"])
    monkeypatch.setattr(
        ss, "_bulk_fetch_daily",
        lambda candidates, days=320: {c: SAMPLES[c] for c in candidates},
    )
    result = ss.scan_custom("找上涨的股票", limit=10)
    assert "error" not in result, result.get("error")
    assert result["ok"] is True
    assert result["cached"] is False
    assert result["total_hits"] == 2
    assert len(result["hits"]) == 2
    assert "def strategy" in result["code"]


def test_scan_custom_cache_hit(monkeypatch, tmp_path):
    """同一句第二次走缓存,不再调 LLM。"""
    monkeypatch.setattr(ss, "CACHE_PATH", tmp_path / "cache.json")
    calls = {"n": 0}

    class _Decider:
        def generate(self, prompt, timeout=90):
            calls["n"] += 1
            return "```python\ndef strategy(df):\n    return True\n```"

    monkeypatch.setattr(ss, "AIDecider", lambda: _Decider())
    monkeypatch.setattr(ss, "_load_samples", lambda: SAMPLES)
    monkeypatch.setattr(ss, "_load_candidates", lambda limit=0: ["600519"])
    monkeypatch.setattr(
        ss, "_bulk_fetch_daily", lambda candidates, days=320: {"600519": SAMPLES["600519"]}
    )
    ss.scan_custom("缓存测试", limit=10)
    assert calls["n"] == 1
    ss.scan_custom("缓存测试", limit=10)
    assert calls["n"] == 1  # 第二次没调 LLM


def test_scan_custom_heal_rounds(monkeypatch, tmp_path):
    """样本验证失败时回灌 LLM 重写(自愈)。"""
    monkeypatch.setattr(ss, "CACHE_PATH", tmp_path / "cache.json")
    outputs = iter([
        "```python\ndef strategy(df):\n    return df['x'][99999]\n```",  # 第 1 轮报错
        "```python\ndef strategy(df):\n    return False\n```",           # 第 2 轮通过
    ])

    class _Decider:
        def generate(self, prompt, timeout=90):
            return next(outputs)

    monkeypatch.setattr(ss, "AIDecider", lambda: _Decider())
    monkeypatch.setattr(ss, "_load_samples", lambda: SAMPLES)
    monkeypatch.setattr(ss, "_load_candidates", lambda limit=0: ["600519"])
    monkeypatch.setattr(
        ss, "_bulk_fetch_daily", lambda candidates, days=320: {"600519": SAMPLES["600519"]}
    )
    result = ss.scan_custom("自愈测试", limit=10)
    assert result["ok"] is True
    assert result["total_hits"] == 0  # strategy 恒 False


def test_scan_custom_all_rounds_fail(monkeypatch, tmp_path):
    """3 轮全失败 → 报错。"""
    monkeypatch.setattr(ss, "CACHE_PATH", tmp_path / "cache.json")

    class _Decider:
        def generate(self, prompt, timeout=90):
            return "```python\ndef strategy(df):\n    return df['x'][99999]\n```"

    monkeypatch.setattr(ss, "AIDecider", lambda: _Decider())
    monkeypatch.setattr(ss, "_load_samples", lambda: SAMPLES)
    result = ss.scan_custom("失败测试", limit=10)
    assert "error" in result
