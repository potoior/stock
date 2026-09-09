"""自定义策略条件层: 指标白名单 + 确定性条件评估 + 编译产物校验。

从 strategy_engine 拆出的纯逻辑(只依赖 pandas,不依赖 config/AI/LLM):
- CONDITION_METRIC_META / METRIC_DESC: 指标白名单与语义说明
- eval_custom_strategy: compiled/buy/sell 结构化条件的确定性判定
- _validate_compiled: AI 编译产物校验

依赖方向(单向,无环):
- strategy_engine -> strategy_conditions (re-export 保持兼容)
- ai_decider     -> strategy_conditions (compile_rule 用 METRIC_DESC)
"""

import pandas as pd

CONDITION_METRIC_META = {
    "price_vs_ma5": "价格 vs MA5",
    "price_vs_ma10": "价格 vs MA10",
    "price_vs_ma20": "价格 vs MA20",
    "price_vs_ma60": "价格 vs MA60",
    "ma5_vs_ma10": "MA5 vs MA10",
    "ma5_vs_ma60": "MA5 vs MA60",
    "ma10_vs_ma60": "MA10 vs MA60",
    "macd_diff_vs_dea": "DIFF vs DEA",
    "macd_above_zero": "MACD零轴上",
    "macd_below_zero": "MACD零轴下",
    "k": "K值",
    "d": "D值",
    "j": "J值",
    "kdj_golden": "KDJ金叉",
    "kdj_death": "KDJ死叉",
    "price_in_boll_upper": "触及BOLL上轨",
    "price_in_boll_lower": "触及BOLL下轨",
    "psy_over": "PSY超买(≥75)",
    "psy_under": "PSY超卖(≤25)",
    "bias_over": "BIAS6超涨(≥3)",
    "bias_under": "BIAS6超跌(≤-3)",
    "pdi_vs_mdi": "PDI vs MDI",
    "sar_bull": "SAR翻红",
    "sar_bear": "SAR翻绿",
    "tower_red": "宝塔线红",
    "tower_green": "宝塔线绿",
    "close_above_open": "阳线收盘",
    "close_below_open": "阴线收盘",
    "volume_expand": "放量(>1.5倍)",
    "volume_shrink": "缩量(<0.7倍)",
    "macd_golden": "MACD金叉",
    "macd_death": "MACD死叉",
    "volume_ratio": "量比(当日量/5日均量)",
}

# 指标语义说明(供规则编译器的 LLM 参考)
METRIC_DESC = {
    "price_vs_ma5": "现价减MA5的差值(>0 表示在MA5上方)",
    "price_vs_ma10": "现价减MA10的差值",
    "price_vs_ma20": "现价减MA20的差值",
    "price_vs_ma60": "现价减MA60的差值",
    "ma5_vs_ma10": "MA5减MA10的差值",
    "ma5_vs_ma60": "MA5减MA60的差值",
    "ma10_vs_ma60": "MA10减MA60的差值",
    "macd_diff_vs_dea": "DIFF减DEA的差值(>0 表示MACD多头)",
    "macd_above_zero": "MACD的DIFF在零轴上方(布尔)",
    "macd_below_zero": "MACD的DIFF在零轴下方(布尔)",
    "k": "KDJ的K值",
    "d": "KDJ的D值",
    "j": "KDJ的J值",
    "kdj_golden": "当日K线上穿D线形成KDJ金叉(布尔事件)",
    "kdj_death": "当日K线下穿D线形成KDJ死叉(布尔事件)",
    "macd_golden": "当日DIFF上穿DEA形成MACD金叉(布尔事件)",
    "macd_death": "当日DIFF下穿DEA形成MACD死叉(布尔事件)",
    "price_in_boll_upper": "现价触及BOLL布林带上轨(布尔)",
    "price_in_boll_lower": "现价触及BOLL布林带下轨(布尔)",
    "psy_over": "PSY心理线≥75超买(布尔)",
    "psy_under": "PSY心理线≤25超卖(布尔)",
    "bias_over": "BIAS6乖离率≥3超涨(布尔)",
    "bias_under": "BIAS6乖离率≤-3超跌(布尔)",
    "pdi_vs_mdi": "DMI的PDI减MDI的差值(>0 表示多头主导)",
    "sar_bull": "价格在SAR上方,SAR翻红(布尔)",
    "sar_bear": "价格在SAR下方,SAR翻绿(布尔)",
    "tower_red": "宝塔线红(布尔)",
    "tower_green": "宝塔线绿(布尔)",
    "close_above_open": "阳线收盘,收盘价高于开盘价(布尔)",
    "close_below_open": "阴线收盘,收盘价低于开盘价(布尔)",
    "volume_expand": "当日成交量大于5日均量1.5倍(布尔)",
    "volume_shrink": "当日成交量小于5日均量0.7倍(布尔)",
    "volume_ratio": "当日成交量除以5日均量的比值(数值,如 1.5)",
}


def _eval_metric(ctx, metric):
    i = ctx["i"]
    df = ctx["df"]
    price = ctx["price"]
    if metric == "price_vs_ma5":
        return price - ctx["ma5"].iloc[i]
    if metric == "price_vs_ma10":
        return price - ctx["ma10"].iloc[i]
    if metric == "price_vs_ma20":
        return price - ctx["ma20"].iloc[i]
    if metric == "price_vs_ma60":
        return price - ctx["ma60"].iloc[i]
    if metric == "ma5_vs_ma10":
        return ctx["ma5"].iloc[i] - ctx["ma10"].iloc[i]
    if metric == "ma5_vs_ma60":
        return ctx["ma5"].iloc[i] - ctx["ma60"].iloc[i]
    if metric == "ma10_vs_ma60":
        return ctx["ma10"].iloc[i] - ctx["ma60"].iloc[i]
    if metric == "macd_diff_vs_dea":
        return ctx["macd_diff"].iloc[i] - ctx["macd_dea"].iloc[i]
    if metric == "macd_above_zero":
        return 1 if ctx["macd_diff"].iloc[i] > 0 else 0
    if metric == "macd_below_zero":
        return 1 if ctx["macd_diff"].iloc[i] < 0 else 0
    if metric == "k":
        return ctx["k"].iloc[i]
    if metric == "d":
        return ctx["d"].iloc[i]
    if metric == "j":
        return ctx["j"].iloc[i]
    if metric == "kdj_golden":
        return (
            1 if ctx["k"].iloc[i] > ctx["d"].iloc[i] and ctx["k"].iloc[i - 1] <= ctx["d"].iloc[i - 1] else 0
        )
    if metric == "kdj_death":
        return (
            1 if ctx["k"].iloc[i] < ctx["d"].iloc[i] and ctx["k"].iloc[i - 1] >= ctx["d"].iloc[i - 1] else 0
        )
    if metric == "price_in_boll_upper":
        return 1 if price >= ctx["boll_u"].iloc[i] else 0
    if metric == "price_in_boll_lower":
        return 1 if price <= ctx["boll_l"].iloc[i] else 0
    if metric == "psy_over":
        return 1 if ctx["psy"].iloc[i] >= 75 else 0
    if metric == "psy_under":
        return 1 if ctx["psy"].iloc[i] <= 25 else 0
    if metric == "bias_over":
        return 1 if ctx["bias1"].iloc[i] >= 3 else 0
    if metric == "bias_under":
        return 1 if ctx["bias1"].iloc[i] <= -3 else 0
    if metric == "pdi_vs_mdi":
        return ctx["pdi"].iloc[i] - ctx["mdi"].iloc[i]
    if metric == "sar_bull":
        return 1 if ctx["sar"][i] > 0 and price > ctx["sar"][i] else 0
    if metric == "sar_bear":
        return 1 if ctx["sar"][i] > 0 and price < ctx["sar"][i] else 0
    if metric == "tower_red":
        return 1 if ctx["tower"].iloc[i] > 0 else 0
    if metric == "tower_green":
        return 1 if ctx["tower"].iloc[i] < 0 else 0
    if metric == "close_above_open":
        return 1 if df["close"].iloc[i] > df["open"].iloc[i] else 0
    if metric == "close_below_open":
        return 1 if df["close"].iloc[i] < df["open"].iloc[i] else 0
    if metric == "volume_expand":
        v = df["volume"].iloc[i]
        avg = df["volume"].iloc[i - 5 : i].mean() if i >= 5 else df["volume"].mean()
        return 1 if avg > 0 and v > avg * 1.5 else 0
    if metric == "volume_shrink":
        v = df["volume"].iloc[i]
        avg = df["volume"].iloc[i - 5 : i].mean() if i >= 5 else df["volume"].mean()
        return 1 if avg > 0 and v < avg * 0.7 else 0
    if metric == "macd_golden":
        return (
            1
            if ctx["macd_diff"].iloc[i] > ctx["macd_dea"].iloc[i]
            and ctx["macd_diff"].iloc[i - 1] <= ctx["macd_dea"].iloc[i - 1]
            else 0
        )
    if metric == "macd_death":
        return (
            1
            if ctx["macd_diff"].iloc[i] < ctx["macd_dea"].iloc[i]
            and ctx["macd_diff"].iloc[i - 1] >= ctx["macd_dea"].iloc[i - 1]
            else 0
        )
    if metric == "volume_ratio":
        v = df["volume"].iloc[i]
        avg = df["volume"].iloc[i - 5 : i].mean() if i >= 5 else df["volume"].mean()
        return v / avg if avg > 0 else 0
    return 0


def eval_condition(ctx, cond):
    """cond: {metric, op, threshold}  op in: >, >=, <, <=, ==, is_true"""
    metric = cond.get("metric")
    op = cond.get("op", ">")
    val = _eval_metric(ctx, metric)
    if pd.isna(val):
        return False
    threshold = cond.get("threshold", 0)
    if op == "is_true":
        return bool(val)
    try:
        if op == ">":
            return val > threshold
        if op == ">=":
            return val >= threshold
        if op == "<":
            return val < threshold
        if op == "<=":
            return val <= threshold
        if op == "==":
            return abs(val - threshold) < 1e-6
    except Exception:
        return False
    return False


def eval_condition_group(ctx, group):
    """条件组求值。group 支持三种形态:
    - [cond, ...]         列表: 全部满足(AND, 兼容旧格式)
    - {"all": [cond,...]} 全部满足
    - {"any": [cond,...]} 任一满足(OR)
    """
    if isinstance(group, list):
        return bool(group) and all(eval_condition(ctx, c) for c in group)
    if isinstance(group, dict):
        if "all" in group:
            return bool(group["all"]) and all(eval_condition(ctx, c) for c in group["all"])
        if "any" in group:
            return any(eval_condition(ctx, c) for c in group["any"])
    return False


def _group_reason(ctx, group):
    """生成条件组的人类可读描述,标注每条条件的当前取值。"""
    joiner = " 且 " if not isinstance(group, dict) else (" 且 " if "all" in group else " 或 ")
    conds = group if isinstance(group, list) else next(iter(group.values()), [])
    parts = []
    for c in conds:
        name = CONDITION_METRIC_META.get(c.get("metric"), c.get("metric"))
        val = _eval_metric(ctx, c.get("metric"))
        if c.get("op") == "is_true" or c.get("op", ">") == "is_true":
            parts.append(f"{name}={'是' if val else '否'}")
        else:
            parts.append(f"{name}={val:.2f}{c.get('op', '>')}{c.get('threshold', 0)}")
    return joiner.join(parts) or "无"


def eval_custom_strategy(ctx, strat):
    """确定性判定自定义策略。

    条件来源优先级:compiled(AI 编译的结构化条件) > buy/sell(手工结构化条件)。
    buy/sell 内条件为 AND;compiled 支持 {"all":[...]} / {"any":[...]} 分组。
    """
    buy = strat.get("compiled", {}).get("buy") or strat.get("buy")
    sell = strat.get("compiled", {}).get("sell") or strat.get("sell")
    if buy and eval_condition_group(ctx, buy):
        return "buy", "买入条件满足: " + _group_reason(ctx, buy)
    if sell and eval_condition_group(ctx, sell):
        return "sell", "卖出条件满足: " + _group_reason(ctx, sell)
    return "hold", "自定义条件未触发"


# ---------------- 规则编译(自然语言 → 结构化条件,AI 只翻译一次) ----------------


def _validate_compiled(compiled: dict) -> str | None:
    """校验编译产物,返回错误信息或 None(通过)。"""
    if not isinstance(compiled, dict):
        return "编译结果必须是 JSON 对象"
    for side in ("buy", "sell"):
        group = compiled.get(side)
        if group is None:
            continue
        if isinstance(group, dict):
            if "all" not in group and "any" not in group:
                return f"{side} 只支持 {{'all':[...]}} / {{'any':[...]}} 形态"
            group = next(iter(group.values()))
        if not isinstance(group, list):
            return f"{side} 条件必须是列表"
        for c in group:
            if not isinstance(c, dict) or c.get("metric") not in CONDITION_METRIC_META:
                bad = c.get("metric") if isinstance(c, dict) else c
                return f"{side} 含未知指标: {bad}"
            if c.get("op", ">") not in (">", ">=", "<", "<=", "==", "is_true"):
                return f"{side} 含未知比较符: {c.get('op')}"
    return None
