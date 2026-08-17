"""strategy_rules.py 离线单元测试

构造不同 K 线形态验证各策略的买卖判断，不依赖网络。
"""

import numpy as np
import pandas as pd


def make_df(closes, highs=None, lows=None):
    n = len(closes)
    closes = pd.Series(closes, dtype=float)
    if highs is None:
        highs = closes + 1
    if lows is None:
        lows = closes - 1
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y%m%d"),
            "open": closes,
            "high": closes + (highs - closes),
            "low": closes + (lows - closes),
            "close": closes,
        }
    )


def run():
    from strategy_rules import (
        judge_atr,
        judge_boll,
        judge_combo,
        judge_dmi,
        judge_kdj,
        judge_ma,
        judge_macd,
    )

    failed = 0

    def test(name, ok, detail=""):
        nonlocal failed
        print(("  ✓ " + name) if ok else ("  ✗ " + name + ": " + detail))
        failed += 0 if ok else 1

    print("1. MACD 金叉/死叉")
    # 金叉落最后一根：横盘30天后单日暴涨
    df_gold = make_df([50.0] * 30 + [60.0])
    act, det = judge_macd(df_gold, 60.0, None)
    test("金叉最后一个bar买入", act == "buy", f"{act} {det}")
    pos = {"qty": 100, "cost": 50.0}
    act, det = judge_macd(df_gold, 60.0, pos)
    test("金叉后持有非卖出", act != "sell", f"{act} {det}")
    # 死叉落最后一根：冲高后连续急跌
    df_dead = make_df([50.0] * 12 + [52, 53, 54, 55, 56, 57, 55, 53, 51, 49])
    act, det = judge_macd(df_dead, 49.0, {"qty": 100, "cost": 54.0})
    test("死叉卖出", act == "sell", f"{act} {det}")

    print("2. KDJ 超卖/超买")
    # 持续超卖区的金叉
    low_vals = list(pd.Series(np.linspace(20, 15, 50)) + np.arange(50) * 0.05)
    df_k = make_df(low_vals)
    act, det = judge_kdj(df_k, float(df_k["close"].iloc[-1]), None, oversold=25, overbought=75)
    print(f"    (KDJ超卖买入判断: {act})")
    test("KDJ 无异常", act in ("buy", "hold"), f"{act}")

    print("3. 5日均线")
    up_ma = make_df(list(pd.Series(np.linspace(50, 80, 60))))
    act, det = judge_ma(up_ma, 60, None, period=5)
    test("上涨突破返回buy/hold", act in ("buy", "hold"), f"{act} {det}")

    print("4. 布林带")
    df_b = make_df(list(pd.Series(np.linspace(30, 60, 80))))
    act, det = judge_boll(df_b, 60, None)
    test("布林无异常", act in ("buy", "hold"), f"{act} {det}")

    print("5. DMI")
    df_d = make_df(list(pd.Series(np.linspace(10, 40, 90))))
    act, det = judge_dmi(df_d, 40, None)
    test("DMI无异常", act in ("buy", "hold"), f"{act} {det}")

    print("6. 三指标共振")
    df_c = make_df(list(pd.Series(np.linspace(20, 50, 100))))
    act, det = judge_combo(df_c, 50, None)
    test("共振无异常", act in ("buy", "hold"), f"{act} {det}")

    print("7. ATR")
    df_a = make_df(list(pd.Series(np.linspace(10, 30, 90))))
    act, det = judge_atr(df_a, 30, None)
    test("ATR无异常", act in ("buy", "hold"), f"{act} {det}")

    print("-" * 40)
    print("FAILED" if failed else "ALL PASS", f"({failed} 失败)")
    return failed


if __name__ == "__main__":
    raise SystemExit(run())
