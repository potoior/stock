"""技术指标计算函数 + 默认策略参数(从 strategy_engine.py 提取)。

包含:
- compute_macd / compute_kdj / compute_boll / compute_psy / compute_bias
- compute_bbiboll / compute_tower / compute_rsi / compute_mos_lows
- compute_dmi / compute_sar
- DEFAULT_STRATEGY_PARAMS: 54 个内置策略的默认参数

被 strategy_engine.py 导入,通过 re-export 保持兼容。
所有函数纯计算(输入 DataFrame,输出 Series),无副作用,线程安全。
"""

import numpy as np
import pandas as pd


def compute_macd(df, fast=12, slow=26, signal=9):
    close = df["close"]
    diff = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = diff.ewm(span=signal, adjust=False).mean()
    bar = (diff - dea) * 2
    return diff, dea, bar


def compute_kdj(df, n=9, k1=3, d1=3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    spread = (high_n - low_n).replace(0, np.nan)
    rsv = (df["close"] - low_n) / spread * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=k1 - 1, adjust=False).mean()
    d = k.ewm(com=d1 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j, rsv


def compute_boll(df, period=20, std=2):
    mid = df["close"].rolling(period).mean()
    sd = df["close"].rolling(period).std(ddof=0)
    return mid + sd * std, mid, mid - sd * std


def compute_psy(df, period=12):
    up = (df["close"] > df["close"].shift(1)).astype(float)
    return up.rolling(period).sum() / period * 100


def compute_bias(df, p1=6, p2=12, p3=24):
    c = df["close"]
    b1 = (c - c.rolling(p1).mean()) / c.rolling(p1).mean() * 100
    b2 = (c - c.rolling(p2).mean()) / c.rolling(p2).mean() * 100
    b3 = (c - c.rolling(p3).mean()) / c.rolling(p3).mean() * 100
    return b1, b2, b3


def compute_bbiboll(df, m1=3, m2=6, m3=12, m4=24, n=11, m=6):
    ma1 = df["close"].rolling(m1).mean()
    ma2 = df["close"].rolling(m2).mean()
    ma3 = df["close"].rolling(m3).mean()
    ma4 = df["close"].rolling(m4).mean()
    bbi = (ma1 + ma2 + ma3 + ma4) / 4
    sd = bbi.rolling(n).std(ddof=0)
    return bbi + sd * m, bbi, bbi - sd * m


def compute_tower(df):
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    n = len(close)
    tower = np.zeros(n)
    for i in range(1, n):
        if close[i] > high[i - 1]:
            tower[i] = 1  # 突破前一根最高价 → 翻红
        elif close[i] < low[i - 1]:
            tower[i] = -1  # 跌破前一根最低价 → 翻绿
        else:
            tower[i] = tower[i - 1]  # 未突破/未跌破 → 延续前态
    return pd.Series(tower, index=df.index)


def compute_rsi(df, p1=6, p2=12):
    """RSI 相对强弱指标，返回 (RSI短线, RSI长线)。"""
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    def _rsi(period):
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        return rsi.fillna(50)

    return _rsi(p1), _rsi(p2)


def compute_mos_lows(df, diff=None, dea=None):
    """MOS 看盘系统（通达信公式精确实现）。

    原公式：
      DIFF:=100*(EMA(CLOSE,12)-EMA(CLOSE,26))
      DEA:=EMA(DIFF,9)
      死叉:=CROSS(DEA,DIFF)
      N1:=BARSLAST(死叉)  N2:=REF(BARSLAST(死叉),N1+1)
      CL1:=LLV(LOW,N1+1)  DIFL1:=LLV(DIFF,N1+1)
      CL2:=REF(CL1,N1+1)  DIFL2:=REF(DIFL1,N1+1)
    返回 dict: cl1/cl2/cl3, difl1/difl2/difl3, n1, 以及
      bottom  = CL1<CL2 且 DIFL1>=DIFL2（底背离低点）
    """
    if diff is None:
        diff, dea, _ = compute_macd(df)
    low = df["low"].values
    dif_arr = diff.values
    n = len(df)

    # 死叉点: DEA 上穿 DIFF
    death = np.zeros(n, dtype=bool)
    for i in range(1, n):
        if dea.iloc[i] > diff.iloc[i] and dea.iloc[i - 1] <= diff.iloc[i - 1]:
            death[i] = True

    if death.sum() == 0:
        return {
            "cl1": None, "cl2": None, "cl3": None,
            "difl1": None, "difl2": None, "difl3": None,
            "n1": n, "bottom": False,
        }

    death_idx = np.where(death)[0]
    last = death_idx[-1]
    n1 = n - 1 - last
    _cl1 = low[last:].min()
    _dl1 = dif_arr[last:].min()
    if len(death_idx) >= 2:
        prev = death_idx[-2]
        cl2 = low[prev : last + 1].min()
        dl2 = dif_arr[prev : last + 1].min()
    else:
        cl2 = low[: last + 1].min()
        dl2 = dif_arr[: last + 1].min()
    if len(death_idx) >= 3:
        pp = death_idx[-3]
        cl3 = low[pp : prev + 1].min()
        dl3 = dif_arr[pp : prev + 1].min()
    else:
        cl3 = low.min()
        dl3 = dif_arr.min()

    return {
        "cl1": float(_cl1),
        "cl2": float(cl2),
        "cl3": float(cl3),
        "difl1": float(_dl1),
        "difl2": float(dl2),
        "difl3": float(dl3),
        "n1": int(n1),
        "bottom": bool(_cl1 < cl2 and _dl1 >= dl2),
    }


def compute_dmi(df, n=14, m=6):
    high, low, close = df["high"], df["low"], df["close"]
    ph, pl, pc = high.shift(1), low.shift(1), close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    hd, ld = high - ph, pl - low
    dmp = pd.Series(np.where((hd > 0) & (hd > ld), hd, 0), index=df.index)
    dmm = pd.Series(np.where((ld > 0) & (ld > hd), ld, 0), index=df.index)
    mtr = tr.rolling(n).sum()
    dmp_s = dmp.rolling(n).sum()
    dmm_s = dmm.rolling(n).sum()
    pdi = pd.Series(np.where(mtr > 0, 100 * dmp_s / mtr, 0), index=df.index)
    mdi = pd.Series(np.where(mtr > 0, 100 * dmm_s / mtr, 0), index=df.index)
    dx = pd.Series(np.where(pdi + mdi > 0, 100 * (pdi - mdi).abs() / (pdi + mdi), 0), index=df.index)
    adx = dx.rolling(m).mean()
    return pdi, mdi, adx


def compute_sar(df, af_init=0.02, af_max=0.2):
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    n = len(close)
    sar = np.zeros(n)
    trend = np.zeros(n)
    af = np.zeros(n)
    ep = np.zeros(n)
    sar[0] = close[0]
    trend[0] = 1 if close[0] >= close[min(4, n - 1)] else -1
    af[0] = af_init
    ep[0] = high[0] if trend[0] >= 0 else low[0]
    for i in range(1, n):
        if trend[i - 1] >= 0:
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])
            if i > 0:
                sar[i] = min(sar[i], low[i - 1])
            if i > 1:
                sar[i] = min(sar[i], low[i - 2])
            if sar[i] > low[i]:
                trend[i] = -1
                sar[i] = ep[i - 1]
                af[i] = af_init
                ep[i] = low[i]
            else:
                trend[i] = 1
                if high[i] > ep[i - 1]:
                    ep[i] = high[i]
                    af[i] = min(af[i - 1] + af_init, af_max)
                else:
                    ep[i] = ep[i - 1]
                    af[i] = af[i - 1]
        else:
            sar[i] = sar[i - 1] + af[i - 1] * (ep[i - 1] - sar[i - 1])
            if i > 0:
                sar[i] = max(sar[i], high[i - 1])
            if i > 1:
                sar[i] = max(sar[i], high[i - 2])
            if sar[i] < high[i]:
                trend[i] = 1
                sar[i] = ep[i - 1]
                af[i] = af_init
                ep[i] = high[i]
            else:
                trend[i] = -1
                if low[i] < ep[i - 1]:
                    ep[i] = low[i]
                    af[i] = min(af[i - 1] + af_init, af_max)
                else:
                    ep[i] = ep[i - 1]
                    af[i] = af[i - 1]
    return sar, trend


# ---------------- 配置持久化 ----------------

DEFAULT_STRATEGY_PARAMS = {
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "kdj": {"n": 9, "k1": 3, "d1": 3},
    "ma_stop": {"period": 5},
    "boll": {"period": 20, "std": 2},
    "dmi": {"n": 14, "m": 6},
    "psy": {"period": 12},
    "bias": {"short": 3, "long": 5},
    "sar": {"af_init": 0.02, "af_max": 0.2},
    "ma_combo": {"short": 5, "mid": 10, "long": 60},
    "two_line": {"short": 5, "long": 10},
    "life_line": {"period": 60},
    "three_third": {"p1": 7, "p2": 13, "p3": 20},
    "sparrow": {"lookback": 5, "target": 2.5},
    "bounce": {"rebound_pct": 0.5, "vol_increase": 20},
    "volume_div": {"lookback": 10, "shrink": 0.7, "expand": 1.3},
    "dmi_psy": {"pdi_threshold": 5, "psy_threshold": 25},
    "rsi": {"p1": 6, "p2": 12, "oversold": 30, "overbought": 70},
    "bottom": {"lookback": 20, "vol_shrink": 0.5, "drop_pct": -5},
    "top": {"lookback": 20, "vol_expand": 2.0, "rise_pct": 5},
    "zt": {"zt_pct": 9.6, "min_vol_ratio": 1.5},
    # 操练大全12章 投资法则
    "trend_follow": {"threshold": 25, "weak": 20},
    "pyramid": {"n": 20, "step": 0.1},
    "stop_profit": {"short": 5, "long": 10, "short_pct": 20, "long_pct": 30},
    "plan_trade": {"ma_period": 10},
    # 漫画书 量能/实战战法
    "high_volume": {"n": 20},
    "demon_stock": {"consec": 3, "consec_pct": 5, "hot": 5, "hot_pct": 30},
    "dragon_pullback": {"lookback": 30, "zt_pct": 9.6, "band": 3, "vol_ratio": 1.5},
    "support_resistance": {"n": 20, "vol_ratio": 1.5},
    "range_trade": {"n": 20, "low_pct": 0.2, "high_pct": 0.2},
    # 操练大全15章 抄底
    "bottom_ma": {},
    # 操练大全16章 逃顶(周/月线)
    "top_weekly": {"rise_pct": 15},
    "top_monthly": {"rise_pct": 25},
    # 操练大全17章 跟庄
    "zhuang_test": {"shadow_ratio": 2, "shrink": 0.7, "low_pct": 0.3, "n": 60},
    "zhuang_build": {"low_pct": 0.3, "vol_ratio": 1.5, "amplitude": 10, "n": 60},
    "zhuang_pull": {"vol_ratio": 2, "rise_pct": 5, "n": 20},
    "zhuang_ship": {"high_pct": 0.7, "vol_ratio": 1.5, "stale_pct": 2, "n": 60},
    "zhuang_wash": {"rise_pct": 10, "shrink": 0.8, "pull_min": -8, "pull_max": -3},
    # 操练大全20章 涨停细分
    "zt_type": {"zt_pct": 9.6, "tolerance": 0.5},
    "zt_unsealed": {"zt_pct": 9.6, "break_pct": 1, "vol_ratio": 2},
    "zt_pull": {"zt_pct": 9.6, "pull_min": 5, "vol_ratio": 2, "body_ratio": 70},
    # 操练大全14章 基本面
    "pe_select": {"low_pe": 15, "high_pe": 50},
    "roe_pe": {"roe_min": 15, "pe_max": 25, "roe_bad": 5, "pe_high": 50},
    # 漫画书 实战战法(剩余)
    "daban": {"zt_pct": 9.6, "min_vol_ratio": 1.5, "consec": 2, "n": 10},
    "fupan": {"n": 30, "zt_pct": 9.6, "support_pct": 5, "resistance_pct": 5, "rise_threshold": 20},
    # 操练大全15章 抄底(剩余)
    "bottom_time": {"n": 120, "tolerance": 2},
    # 操练大全14章 选股(剩余)
    "shareholder_select": {"concentrate": -5, "disperse": 10},
    "policy_select": {"num": 10, "min_positive": 2, "min_negative": 2},
    # 经典 K 线形态 + 顶背离 + 缺口
    "kline_pattern": {},
    "macd_top_divergence": {"n": 60},
    "rsi_top_divergence": {"n": 60},
    "gap": {"gap_pct": 1.0, "vol_ratio": 1.5, "n": 20, "exhaustion_lookback": 5, "exhaustion_cum_pct": 20},
}
