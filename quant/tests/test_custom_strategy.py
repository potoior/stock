"""自定义策略解耦测试:规则编译 + 确定性判定。

策略解耦设计:
- AI 只做一次性翻译(自然语言 → 结构化条件)
- 判定走纯代码(eval_custom_strategy),LLM 不参与,100% 可复现
"""


import pandas as pd
import pytest

import strategy_engine as se


def make_ctx(price=10.0, ma5=9.0, ma10=8.0, k=20.0, d=30.0, macd_diff=0.1, macd_dea=0.05):
    """构造最小 ctx(常数序列,指标值可预期)。"""
    n = 30

    def s(v):
        return pd.Series([v] * n)

    ind = {
        "macd_diff": macd_diff, "macd_dea": macd_dea, "macd_bar": macd_diff - macd_dea,
        "k": k, "d": d, "j": 2 * k - d,
        "boll_u": price + 1, "boll_m": price, "boll_l": price - 1,
        "bbiboll_u": price + 1, "bbiboll_m": price, "bbiboll_l": price - 1,
        "ma5": ma5, "ma10": ma10, "ma20": ma10, "ma60": ma10,
        "psy": 80, "bias1": 4, "bias2": 4, "bias3": 4,
        "pdi": 20, "mdi": 10, "adx": 25, "sar": price - 0.5,
        "tower": 1, "rsi6": 50, "rsi12": 50,
    }
    return {
        "i": n - 1,
        "price": price,
        "code": "600519",
        "df": pd.DataFrame({"open": [price] * n, "close": [price] * n, "volume": [100] * n}),
        "close": s(price),
        "ma5": s(ma5),
        "ma10": s(ma10),
        "ma20": s(ma10),
        "ma60": s(ma10),
        "macd_diff": s(macd_diff),
        "macd_dea": s(macd_dea),
        "k": s(k),
        "d": s(d),
        "j": s(2 * k - d),
        "boll_u": s(price + 1),
        "boll_m": s(price),
        "boll_l": s(price - 1),
        "psy": s(80.0),
        "bias1": s(4.0),
        "bias2": s(4.0),
        "bias3": s(4.0),
        "pdi": s(20.0),
        "mdi": s(10.0),
        "sar": s(price - 0.5),
        "tower": s(1.0),
        "rsi6": s(50.0),
        "rsi12": s(50.0),
        "indicators": ind,
    }


# ============ eval_condition_group ============


def test_condition_group_list_all():
    ctx = make_ctx()
    conds = [
        {"metric": "price_vs_ma5", "op": ">"},
        {"metric": "k", "op": "<", "threshold": 50},
    ]
    assert se.eval_condition_group(ctx, conds)  # AND: 都满足
    assert not se.eval_condition_group(ctx, [])  # 空条件组不触发


def test_condition_group_any():
    ctx = make_ctx(k=80, d=90)  # K 未金叉且超买
    group = {"any": [
        {"metric": "kdj_golden"},
        {"metric": "k", "op": ">", "threshold": 50},
    ]}
    assert se.eval_condition_group(ctx, group)  # OR: K>50 成立即可


def test_condition_group_all_dict():
    ctx = make_ctx(price=8.5, ma5=9.0)
    group = {"all": [{"metric": "price_vs_ma5", "op": ">"}]}
    assert not se.eval_condition_group(ctx, group)  # 价格在 MA5 下方


# ============ eval_custom_strategy(compiled) ============


def test_eval_compiled_all():
    strat = {
        "name": "测试",
        "compiled": {
            "buy": {"all": [{"metric": "price_vs_ma5", "op": ">"}, {"metric": "macd_golden"}]},
            "sell": {"any": [{"metric": "kdj_death"}]},
        },
    }
    ctx = make_ctx()
    # 用交叉序列构造 MACD 金叉
    ctx["macd_diff"] = pd.Series([0.05] * 29 + [0.1])
    ctx["macd_dea"] = pd.Series([0.06] * 29 + [0.05])
    sig, reason = se.eval_custom_strategy(ctx, strat)
    assert sig == "buy"
    assert "买入条件满足" in reason


def test_eval_compiled_any_sell():
    strat = {
        "name": "测试",
        "compiled": {
            "buy": {"all": [{"metric": "kdj_golden"}]},
            "sell": {"any": [{"metric": "k", "op": ">", "threshold": 50}, {"metric": "kdj_death"}]},
        },
    }
    ctx = make_ctx(k=80, d=90)
    sig, _ = se.eval_custom_strategy(ctx, strat)
    assert sig == "sell"  # any: K>50 即触发卖出


def test_eval_compiled_not_triggered():
    strat = {
        "name": "测试",
        "compiled": {
            "buy": {"all": [{"metric": "price_vs_ma5", "op": ">"}]},
            "sell": {"any": [{"metric": "price_vs_ma10", "op": ">"}]},
        },
    }
    ctx = make_ctx(price=8.5, ma5=9.0, ma10=10.5)
    sig, reason = se.eval_custom_strategy(ctx, strat)
    assert sig == "hold"
    assert "未触发" in reason


def test_eval_old_format_still_works():
    """旧格式 buy/sell 列表(AND)保持兼容。"""
    strat = {
        "name": "旧格式",
        "buy": [{"metric": "price_vs_ma5", "op": ">"}],
        "sell": [],
    }
    ctx = make_ctx()
    sig, _ = se.eval_custom_strategy(ctx, strat)
    assert sig == "buy"


# ============ _validate_compiled ============


def test_validate_compiled_ok():
    assert se._validate_compiled({
        "buy": {"all": [{"metric": "price_vs_ma5", "op": ">"}]},
        "sell": {"any": [{"metric": "kdj_golden"}]},
    }) is None


def test_validate_compiled_bad_metric():
    res = se._validate_compiled({"buy": {"all": [{"metric": "not_exist"}]}})
    assert res and "未知指标" in res


def test_validate_compiled_bad_op():
    res = se._validate_compiled({"sell": {"all": [{"metric": "k", "op": "~~"}]}})
    assert res and "比较符" in res


# ============ compile_custom_strategy ============


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """临时 config.json + 清空配置缓存。"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(se, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(se, "_config_cache", {"mtime": 0, "data": None})
    se.save_strategies([
        {
            "id": "custom_ai_demo",
            "name": "放量突破",
            "type": "custom",
            "enabled": True,
            "buy_rule": "MACD金叉且放量,价格站上MA5",
            "sell_rule": "跌破MA10",
        }
    ])
    yield cfg_path
    monkeypatch.setattr(se, "_config_cache", {"mtime": 0, "data": None})


def test_compile_success(tmp_config, monkeypatch):
    class FakeDecider:
        def compile_rule(self, name, buy_rule, sell_rule):
            return {"compiled": {
                "buy": {"all": [{"metric": "macd_golden"}, {"metric": "price_vs_ma5", "op": ">"}]},
                "sell": {"any": [{"metric": "price_vs_ma10", "op": "<"}]},
            }}
    monkeypatch.setattr("ai_decider.AIDecider", FakeDecider)

    res = se.compile_custom_strategy("custom_ai_demo")
    assert res["ok"]
    # 编译结果持久化
    strat = next(s for s in se.get_strategies() if s["id"] == "custom_ai_demo")
    assert "compiled" in strat
    assert strat["compiled"]["buy"]["all"][0]["metric"] == "macd_golden"


def test_compile_invalid_result_rejected(tmp_config, monkeypatch):
    class FakeDecider:
        def compile_rule(self, name, buy_rule, sell_rule):
            return {"compiled": {"buy": {"all": [{"metric": "不存在的指标"}]}}}
    monkeypatch.setattr("ai_decider.AIDecider", FakeDecider)

    res = se.compile_custom_strategy("custom_ai_demo")
    assert not res["ok"]
    # 校验失败不应写入
    strat = next(s for s in se.get_strategies() if s["id"] == "custom_ai_demo")
    assert "compiled" not in strat


def test_compile_not_found(tmp_config):
    res = se.compile_custom_strategy("nonexistent")
    assert not res["ok"]


# ============ judge_custom_with_ai 分流 ============


def test_judge_compiled_never_calls_ai(tmp_config, monkeypatch):
    """有 compiled 的策略必须走确定性代码,不能碰 AI。"""
    class MustNotCall:
        def judge_code(self, *a, **kw):
            raise AssertionError("compiled 策略不应调用 AI")

    monkeypatch.setattr("ai_decider.AIDecider", MustNotCall)

    strat = {
        "id": "custom_ai_demo",
        "name": "放量突破",
        "type": "custom",
        "buy_rule": "x",
        "sell_rule": "y",
        "compiled": {
            "buy": {"all": [{"metric": "price_vs_ma5", "op": ">"}]},
            "sell": {"any": [{"metric": "kdj_death"}]},
        },
    }
    out = se.judge_custom_with_ai("600519", make_ctx(), [strat], use_ai=True)
    assert len(out) == 1
    assert out[0]["signal"] == "buy"
    assert out[0]["ai"] is False


def test_judge_nl_rule_falls_to_ai(tmp_config, monkeypatch):
    """只有自然语言规则的策略仍走 AI 判定。"""

    class FakeDecider:
        def __init__(self):
            pass

        def judge_code(self, code, name, ind_text, rules):
            return {"results": [{"id": r["id"], "signal": "sell", "reason": "AI判定"} for r in rules]}

    monkeypatch.setattr("ai_decider.AIDecider", FakeDecider)

    strat = {
        "id": "custom_ai_demo",
        "name": "放量突破",
        "type": "custom",
        "buy_rule": "某条件",
        "sell_rule": "某条件",
    }
    out = se.judge_custom_with_ai("600519", make_ctx(), [strat], use_ai=True)
    assert len(out) == 1
    assert out[0]["signal"] == "sell"
    assert out[0]["ai"] is True


def test_judge_mixed_strategies(tmp_config, monkeypatch):
    """混合场景:compiled 走代码,NL 走 AI。"""

    class FakeDecider:
        def __init__(self):
            pass

        def judge_code(self, code, name, ind_text, rules):
            return {"results": [{"id": r["id"], "signal": "hold", "reason": "AI判定"} for r in rules]}

    monkeypatch.setattr("ai_decider.AIDecider", FakeDecider)

    compiled_strat = {
        "id": "s_compiled",
        "name": "已编译",
        "type": "custom",
        "compiled": {"buy": {"all": [{"metric": "price_vs_ma5", "op": ">"}]}},
    }
    nl_strat = {"id": "s_nl", "name": "未编译", "type": "custom", "buy_rule": "x", "sell_rule": "y"}
    out = se.judge_custom_with_ai("600519", make_ctx(), [compiled_strat, nl_strat], use_ai=True)
    assert len(out) == 2
    by_id = {r["key"]: r for r in out}
    assert by_id["s_compiled"]["ai"] is False
    assert by_id["s_compiled"]["signal"] == "buy"
    assert by_id["s_nl"]["ai"] is True
