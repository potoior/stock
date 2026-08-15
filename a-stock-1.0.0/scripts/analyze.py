import sys
import math
import pandas as pd
import numpy as np
from datetime import datetime
from data_fetcher import fetch_realtime, get_daily_data

def compute_macd(df, fast=12, slow=26, signal=9):
    close = df["close"]
    exp1 = close.ewm(span=fast, adjust=False).mean()
    exp2 = close.ewm(span=slow, adjust=False).mean()
    diff = exp1 - exp2
    dea = diff.ewm(span=signal, adjust=False).mean()
    bar = (diff - dea) * 2
    return diff, dea, bar

def compute_kdj(df, n=9, k1=3, d1=3):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n) * 100
    k = rsv.ewm(com=k1 - 1, adjust=False).mean()
    d = k.ewm(com=d1 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j, rsv

def compute_boll(df, period=26, std=2):
    mid = df["close"].rolling(period).mean()
    upper = mid + df["close"].rolling(period).std() * std
    lower = mid - df["close"].rolling(period).std() * std
    return upper, mid, lower

def compute_psy(df, period=12):
    up_days = ((df["close"] > df["close"].shift(1))).rolling(period).sum()
    return up_days / period * 100

def compute_bias(df, period=5):
    ma = df["close"].rolling(period).mean()
    return (df["close"] - ma) / ma * 100

def compute_bbiboll(df, m1=3, m2=6, m3=12, m4=24, period=6, std=2):
    ma1 = df["close"].rolling(m1).mean()
    ma2 = df["close"].rolling(m2).mean()
    ma3 = df["close"].rolling(m3).mean()
    ma4 = df["close"].rolling(m4).mean()
    bbi = (ma1 + ma2 + ma3 + ma4) / 4
    sd = df["close"].rolling(period).std()
    upr = bbi + sd * std
    dwn = bbi - sd * std
    return upr, bbi, dwn

def compute_tower(df):
    up = (df["close"] > df["close"].shift(1)).astype(int)
    down = (df["close"] < df["close"].shift(1)).astype(int)
    tower = up - down
    return tower

def compute_dmi(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    up_move = high - prev_high
    down_move = prev_low - low
    pdm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0), index=df.index)
    mdm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0), index=df.index)
    def wilder_smooth(series, period):
        smoothed = series.copy()
        cum_sum = series.iloc[:period].sum()
        if cum_sum == 0 or period == 0:
            return smoothed
        for i in range(period):
            smoothed.iloc[i] = cum_sum / period
        for i in range(period, len(series)):
            smoothed.iloc[i] = smoothed.iloc[i - 1] - smoothed.iloc[i - 1] / period + series.iloc[i]
        return smoothed
    tr_s = wilder_smooth(tr, period)
    pdm_s = wilder_smooth(pdm, period)
    mdm_s = wilder_smooth(mdm, period)
    pdi = pd.Series(np.where(tr_s > 0, 100 * pdm_s / tr_s, 0), index=df.index)
    mdi = pd.Series(np.where(tr_s > 0, 100 * mdm_s / tr_s, 0), index=df.index)
    dx = pd.Series(np.where(pdi + mdi > 0, 100 * (pdi - mdi).abs() / (pdi + mdi), 0), index=df.index)
    adx = dx.rolling(period).mean()
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
    for i in range(1, min(5, n)):
        trend[i] = trend[0]
    for i in range(1, n):
        if trend[i - 1] == 1:
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

def analyze_stock(code):
    rt = fetch_realtime([code])
    if not rt:
        return {"error": f"无法获取 {code} 实时数据"}
    r = rt[0]
    df = get_daily_data(code, days=180)
    if len(df) < 30:
        return {"error": f"{code} 历史数据不足"}
    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"]
    macd_diff, macd_dea, macd_bar = compute_macd(df)
    k, d, j, rsv = compute_kdj(df)
    boll_u, boll_m, boll_l = compute_boll(df)
    psy = compute_psy(df)
    bias = compute_bias(df)
    pdi, mdi, adx = compute_dmi(df)
    sar, trend = compute_sar(df)
    bbiboll_u, bbiboll_m, bbiboll_l = compute_bbiboll(df)
    tower = compute_tower(df)
    df["ma5"] = close.rolling(5).mean()
    df["ma10"] = close.rolling(10).mean()
    df["ma20"] = close.rolling(20).mean()
    df["ma60"] = close.rolling(60).mean()
    df["ma7"] = close.rolling(7).mean()
    df["ma13"] = close.rolling(13).mean()
    i = len(df) - 1
    price = close.iloc[i]
    signals = []
    def add(name, signal, reason):
        signals.append({"name": name, "signal": signal, "reason": reason})

    # 1. MACD金叉死叉
    try:
        diff = macd_diff.iloc[i]
        dea = macd_dea.iloc[i]
        prev_diff = macd_diff.iloc[i - 1]
        prev_dea = macd_dea.iloc[i - 1]
        golden = prev_diff <= prev_dea and diff > dea
        death = prev_diff >= prev_dea and diff < dea
        above_zero = diff > 0 and dea > 0
        below_zero = diff < 0 and dea < 0
        if golden and above_zero:
            add("MACD金叉死叉", "buy", f"零上金叉，DIFF({diff:.2f})上穿DEA({dea:.2f})，锦上添花")
        elif golden and below_zero:
            prev_cross = 0
            for t in range(i - 20, i):
                if t >= 1 and macd_diff.iloc[t] > macd_dea.iloc[t] and macd_diff.iloc[t - 1] < macd_dea.iloc[t - 1]:
                    prev_cross += 1
            if prev_cross >= 1:
                add("MACD金叉死叉", "buy", f"多次零下金叉，DIFF({diff:.2f})上穿DEA({dea:.2f})，可靠性高")
            else:
                add("MACD金叉死叉", "buy", f"零下金叉，DIFF({diff:.2f})上穿DEA({dea:.2f})，可能反弹")
        elif golden:
            add("MACD金叉死叉", "buy", f"金叉，DIFF({diff:.2f})上穿DEA({dea:.2f})")
        elif death and below_zero:
            add("MACD金叉死叉", "sell", f"零下死叉，DIFF({diff:.2f})下穿DEA({dea:.2f})，继续下跌")
        elif death and above_zero:
            add("MACD金叉死叉", "hold", f"零上死叉，多头回调，幅度不会太大，观望")
        else:
            add("MACD金叉死叉", "hold", f"无金叉死叉，DIFF({diff:.2f}) DEA({dea:.2f})")
    except Exception:
        add("MACD金叉死叉", "hold", "数据不足")

    # 2. KDJ超买超卖
    try:
        k_val = k.iloc[i]
        d_val = d.iloc[i]
        if k_val < 10 and d_val < 20:
            add("KDJ超买超卖", "buy", f"超卖区金叉，K={k_val:.1f} D={d_val:.1f}，超卖严重")
        elif k_val > 90 and d_val > 80:
            add("KDJ超买超卖", "sell", f"超买区死叉，K={k_val:.1f} D={d_val:.1f}，超买严重")
        elif k_val > 80:
            add("KDJ超买超卖", "sell", f"K={k_val:.1f}，超买区")
        elif k_val < 20:
            add("KDJ超买超卖", "hold", f"K={k_val:.1f} 超卖区，等待金叉确认")
        else:
            add("KDJ超买超卖", "hold", f"K={k_val:.1f}，中位运行，观望")
    except Exception:
        add("KDJ超买超卖", "hold", "数据不足")

    # 3. 5日均线止损
    try:
        ma5 = df["ma5"].iloc[i]
        if pd.notna(ma5):
            if price > ma5:
                add("5日均线止损", "buy", f"价格({price:.2f})站上MA5({ma5:.2f})")
            else:
                add("5日均线止损", "sell", f"价格({price:.2f})跌破MA5({ma5:.2f})")
        else:
            add("5日均线止损", "hold", "MA5数据不足")
    except Exception:
        add("5日均线止损", "hold", "数据不足")

    # 4. BOLL布林线
    try:
        bu = boll_u.iloc[i]
        bm = boll_m.iloc[i]
        bl = boll_l.iloc[i]
        if pd.notna(bl) and price <= bl:
            add("BOLL布林线", "buy", f"价格({price:.2f})触及下轨({bl:.2f})")
        elif pd.notna(bu) and price >= bu:
            add("BOLL布林线", "sell", f"价格({price:.2f})触及上轨({bu:.2f})")
        elif pd.notna(bm) and price > bm:
            add("BOLL布林线", "buy", f"价格({price:.2f})在中轨({bm:.2f})上方，可操作趋势")
        elif pd.notna(bm):
            add("BOLL布林线", "sell", f"价格({price:.2f})在中轨({bm:.2f})下方，不建议操作")
        else:
            add("BOLL布林线", "hold", "BOLL数据不足")
    except Exception:
        add("BOLL布林线", "hold", "数据不足")

    # 5. DMI趋势（含四线低于50的盘整判断）
    try:
        pdi_v = pdi.iloc[i]
        mdi_v = mdi.iloc[i]
        adx_v = adx.iloc[i]
        if pd.notna(pdi_v) and pd.notna(mdi_v) and pd.notna(adx_v):
            if pdi_v < 50 and mdi_v < 50 and adx_v < 50:
                add("DMI趋势", "hold", f"四线均低于50({pdi_v:.0f}/{mdi_v:.0f}/{adx_v:.0f})，盘整观望")
            elif pdi_v > mdi_v:
                add("DMI趋势", "buy", f"PDI({pdi_v:.1f})>MDI({mdi_v:.1f})，多方主导")
            else:
                add("DMI趋势", "sell", f"MDI({mdi_v:.1f})>PDI({pdi_v:.1f})，空方主导")
        else:
            add("DMI趋势", "hold", "DMI数据不足")
    except Exception:
        add("DMI趋势", "hold", "数据不足")

    # 6. PSY心理线
    try:
        psy_v = psy.iloc[i]
        if pd.notna(psy_v):
            if psy_v <= 25:
                add("PSY心理线", "buy", f"PSY={psy_v:.0f}，超卖区，市场悲观，有望反弹")
            elif psy_v >= 75:
                add("PSY心理线", "sell", f"PSY={psy_v:.0f}，超买区，短期获利盘较多")
            else:
                add("PSY心理线", "hold", f"PSY={psy_v:.0f}，25-75正常区间")
        else:
            add("PSY心理线", "hold", "PSY数据不足")
    except Exception:
        add("PSY心理线", "hold", "数据不足")

    # 7. BIAS乖离率
    try:
        bias_v = bias.iloc[i]
        if pd.notna(bias_v):
            if bias_v <= -3:
                add("BIAS乖离率", "buy", f"BIAS={bias_v:.1f}%，超跌")
            elif bias_v >= 3:
                add("BIAS乖离率", "sell", f"BIAS={bias_v:.1f}%，超涨，注意回调")
            else:
                add("BIAS乖离率", "hold", f"BIAS={bias_v:.1f}%，±3%内正常")
        else:
            add("BIAS乖离率", "hold", "BIAS数据不足")
    except Exception:
        add("BIAS乖离率", "hold", "数据不足")

    # 8. SAR止损
    try:
        sar_v = sar[i]
        if sar_v > 0:
            if price > sar_v:
                add("SAR止损", "buy", f"价格({price:.2f})>SAR({sar_v:.2f})，翻红")
            else:
                add("SAR止损", "sell", f"价格({price:.2f})<SAR({sar_v:.2f})，翻绿")
        else:
            add("SAR止损", "hold", "SAR数据不足")
    except Exception:
        add("SAR止损", "hold", "数据不足")

    # 9. 三指标共振
    try:
        diff = macd_diff.iloc[i]
        dea = macd_dea.iloc[i]
        prev_diff = macd_diff.iloc[i - 1]
        prev_dea = macd_dea.iloc[i - 1]
        k_val = k.iloc[i]
        d_val = d.iloc[i]
        k_prev = k.iloc[i - 1]
        d_prev = d.iloc[i - 1]
        ma5_v = df["ma5"].iloc[i]
        bm = boll_m.iloc[i]
        macd_golden = prev_diff <= prev_dea and diff > dea
        kdj_golden = k_prev <= d_prev and k_val > d_val
        if pd.notna(ma5_v) and pd.notna(bm) and macd_golden and kdj_golden and price > bm and price > ma5_v:
            add("三指标共振", "buy", f"MACD金叉+KDJ金叉+BOLL中轨+站上MA5({ma5_v:.2f})")
        else:
            add("三指标共振", "hold", "MACD+KDJ+BOLL+MA5未同向共振")
    except Exception:
        add("三指标共振", "hold", "数据不足")

    # 10. 均线组合(5/10/60)
    try:
        ma5_v = df["ma5"].iloc[i]
        ma10_v = df["ma10"].iloc[i]
        ma60_v = df["ma60"].iloc[i]
        if pd.notna(ma5_v) and pd.notna(ma10_v) and pd.notna(ma60_v):
            if price > ma5_v > ma10_v > ma60_v:
                add("均线组合", "buy", f"多头排列，{ma5_v:.2f}>{ma10_v:.2f}>{ma60_v:.2f}")
            elif price < ma5_v or price < ma10_v:
                add("均线组合", "sell", f"均线走坏，价格({price:.2f})跌破MA5({ma5_v:.2f})或MA10({ma10_v:.2f})")
            else:
                add("均线组合", "hold", "均线未成多头排列也未见走坏")
        else:
            add("均线组合", "hold", "均线数据不足")
    except Exception:
        add("均线组合", "hold", "数据不足")

    # 11. 量价背离
    try:
        lookback = 10
        if i >= lookback:
            recent_high = close.iloc[i - lookback:i].max()
            recent_vol = df["volume"].iloc[i - lookback:i]
            avg_vol = recent_vol.mean()
            vol_now = df["volume"].iloc[i]
            if price >= recent_high and vol_now < avg_vol * 0.7:
                add("量价背离", "sell", f"价格创新高但量萎缩{vol_now:.0f}<均量{avg_vol:.0f}，无量上涨需警惕")
            elif price >= recent_high and vol_now > avg_vol * 1.3:
                add("量价背离", "buy", f"价格创新高且放量{vol_now:.0f}>均量{avg_vol:.0f}，量价配合")
            else:
                add("量价背离", "hold", "价格未创新高，无量价背离")
        else:
            add("量价背离", "hold", "数据不足")
    except Exception:
        add("量价背离", "hold", "数据不足")

    # 12. DMI+PSY超跌反弹
    try:
        pdi_v = pdi.iloc[i]
        psy_v = psy.iloc[i]
        if pd.notna(pdi_v) and pd.notna(psy_v):
            if pdi_v < 5 and psy_v <= 25:
                add("DMI+PSY超跌", "buy", f"PDI({pdi_v:.1f})<5且PSY({psy_v:.0f})≤25，超跌反弹")
            else:
                add("DMI+PSY超跌", "hold", f"PDI({pdi_v:.1f})/PSY({psy_v:.0f})未达超跌极值")
        else:
            add("DMI+PSY超跌", "hold", "数据不足")
    except Exception:
        add("DMI+PSY超跌", "hold", "数据不足")

    # 13. 三分法(7/13/20)
    try:
        ma7_v = df["ma7"].iloc[i]
        ma13_v = df["ma13"].iloc[i]
        ma20_v = df["ma20"].iloc[i]
        if pd.notna(ma7_v) and pd.notna(ma13_v) and pd.notna(ma20_v):
            if price > ma7_v and price > ma13_v and price > ma20_v:
                add("三分法", "buy", f"站上7日({ma7_v:.2f})/13日({ma13_v:.2f})/20日({ma20_v:.2f})线")
            elif price < ma7_v:
                add("三分法", "sell", f"跌破7日线({ma7_v:.2f})，注意分批减仓")
            else:
                add("三分法", "hold", f"价格({price:.2f})在7/13/20日线之间")
        else:
            add("三分法", "hold", "均线数据不足")
    except Exception:
        add("三分法", "hold", "数据不足")

    # 14. 麻雀战术（2.5%止盈纪律）
    try:
        if i >= 5:
            low5 = close.iloc[i - 5:i].min()
            pnl_from_low = (price - low5) / low5 * 100 if low5 > 0 else 0
            if pnl_from_low >= 2.5:
                add("麻雀战术", "sell", f"自5日低点({low5:.2f})已涨{pnl_from_low:.1f}%≥2.5%，见好就收")
            else:
                add("麻雀战术", "hold", f"自5日低点仅涨{pnl_from_low:.1f}%，未达2.5%止盈线")
        else:
            add("麻雀战术", "hold", "数据不足")
    except Exception:
        add("麻雀战术", "hold", "数据不足")

    # 15. 反弹量化
    try:
        if i >= 3:
            prev_close = close.iloc[i - 1]
            prev_prev = close.iloc[i - 2]
            prev_volume = df["volume"].iloc[i - 1]
            vol_now = df["volume"].iloc[i]
            yesterday_change = (prev_close - prev_prev) / prev_prev * 100
            if yesterday_change < 0:
                today_change = (price - prev_close) / prev_close * 100
                vol_change = (vol_now - prev_volume) / prev_volume * 100 if prev_volume > 0 else 0
                if today_change > abs(yesterday_change) * 0.5 and vol_change > 20:
                    add("反弹量化", "buy", f"涨幅{today_change:.1f}%>昨日跌幅{abs(yesterday_change):.1f}%×50%，放量{vol_change:.0f}%")
                else:
                    add("反弹量化", "hold", f"今日反弹{today_change:.1f}%未达昨日跌幅{abs(yesterday_change):.1f}%的50%或未放量")
            else:
                add("反弹量化", "hold", "昨日非下跌，无反弹条件")
        else:
            add("反弹量化", "hold", "数据不足")
    except Exception:
        add("反弹量化", "hold", "数据不足")

    # 16. 二线法(5/10)
    try:
        ma5_v = df["ma5"].iloc[i]
        ma10_v = df["ma10"].iloc[i]
        if pd.notna(ma5_v) and pd.notna(ma10_v):
            if ma5_v > ma10_v:
                add("二线法", "buy", f"MA5({ma5_v:.2f})>MA10({ma10_v:.2f})，短线可操作")
            else:
                add("二线法", "sell", f"MA5({ma5_v:.2f})<MA10({ma10_v:.2f})，清仓观望")
        else:
            add("二线法", "hold", "均线数据不足")
    except Exception:
        add("二线法", "hold", "数据不足")

    # 17. 60日生命线
    try:
        ma60_v = df["ma60"].iloc[i]
        if pd.notna(ma60_v):
            if price > ma60_v:
                add("60日生命线", "buy", f"价格({price:.2f})在MA60({ma60_v:.2f})上方，积极做多")
            else:
                add("60日生命线", "sell", f"价格({price:.2f})在MA60({ma60_v:.2f})下方，空头市场")
        else:
            add("60日生命线", "hold", "MA60数据不足")
    except Exception:
        add("60日生命线", "hold", "数据不足")

    # 18. BBIBOLL多空布林
    try:
        bu = bbiboll_u.iloc[i]
        bm = bbiboll_m.iloc[i]
        bl = bbiboll_l.iloc[i]
        if pd.notna(bm):
            if price >= bu:
                add("BBIBOLL", "sell", f"价格({price:.2f})突破上轨({bu:.2f})，大概率回调")
            elif price <= bl:
                add("BBIBOLL", "buy", f"价格({price:.2f})跌破下轨({bl:.2f})，大概率反弹")
            elif price > bm:
                add("BBIBOLL", "buy", f"价格({price:.2f})在BBI中轨({bm:.2f})上方，多方强势")
            else:
                add("BBIBOLL", "sell", f"价格({price:.2f})在BBI中轨({bm:.2f})下方，空方强势")
        else:
            add("BBIBOLL", "hold", "BBIBOLL数据不足")
    except Exception:
        add("BBIBOLL", "hold", "数据不足")

    # 19. 宝塔线TOWER
    try:
        tw = tower.iloc[i]
        pre = tower.iloc[i - 1] if i > 0 else 0
        if tw == 1 and pre == -1:
            add("宝塔线", "buy", f"翻红，价格站上前收盘，买入")
        elif tw == -1 and pre == 1:
            add("宝塔线", "sell", f"翻绿，价格跌破前收盘，卖出")
        elif tw == 1:
            add("宝塔线", "buy", f"连续红柱，持仓")
        elif tw == -1:
            add("宝塔线", "sell", f"连续绿柱，持续下跌")
        else:
            add("宝塔线", "hold", "宝塔线走平")
    except Exception:
        add("宝塔线", "hold", "数据不足")
    buy_count = sum(1 for s in signals if s["signal"] == "buy")
    sell_count = sum(1 for s in signals if s["signal"] == "sell")
    hold_count = sum(1 for s in signals if s["signal"] == "hold")
    if buy_count > sell_count and buy_count >= 3:
        verdict = "买入"
        verdict_icon = "⬆"
    elif sell_count > buy_count and sell_count >= 3:
        verdict = "卖出"
        verdict_icon = "⬇"
    else:
        verdict = "观望"
        verdict_icon = "⏸"
    buy_reasons = [s for s in signals if s["signal"] == "buy"]
    sell_reasons = [s for s in signals if s["signal"] == "sell"]
    hold_reasons = [s for s in signals if s["signal"] == "hold"]
    return {
        "realtime": r,
        "indicators": {
            "macd_diff": round(macd_diff.iloc[i], 3) if i < len(macd_diff) else 0,
            "macd_dea": round(macd_dea.iloc[i], 3) if i < len(macd_dea) else 0,
            "macd_bar": round(macd_bar.iloc[i], 3) if i < len(macd_bar) else 0,
            "k": round(k.iloc[i], 1) if i < len(k) else 0,
            "d": round(d.iloc[i], 1) if i < len(d) else 0,
            "j": round(j.iloc[i], 1) if i < len(j) else 0,
            "boll_u": round(boll_u.iloc[i], 2) if i < len(boll_u) else 0,
            "boll_m": round(boll_m.iloc[i], 2) if i < len(boll_m) else 0,
            "boll_l": round(boll_l.iloc[i], 2) if i < len(boll_l) else 0,
            "ma5": round(df["ma5"].iloc[i], 2) if pd.notna(df["ma5"].iloc[i]) else 0,
            "ma10": round(df["ma10"].iloc[i], 2) if pd.notna(df["ma10"].iloc[i]) else 0,
            "ma60": round(df["ma60"].iloc[i], 2) if pd.notna(df["ma60"].iloc[i]) else 0,
            "psy": round(psy.iloc[i], 0) if pd.notna(psy.iloc[i]) else 0,
            "bias": round(bias.iloc[i], 1) if pd.notna(bias.iloc[i]) else 0,
            "pdi": round(pdi.iloc[i], 1) if pd.notna(pdi.iloc[i]) else 0,
            "mdi": round(mdi.iloc[i], 1) if pd.notna(mdi.iloc[i]) else 0,
            "adx": round(adx.iloc[i], 1) if pd.notna(adx.iloc[i]) else 0,
            "sar": round(sar[i], 2) if sar[i] > 0 else 0,
            "bbiboll_u": round(bbiboll_u.iloc[i], 2) if pd.notna(bbiboll_u.iloc[i]) else 0,
            "bbiboll_m": round(bbiboll_m.iloc[i], 2) if pd.notna(bbiboll_m.iloc[i]) else 0,
            "bbiboll_l": round(bbiboll_l.iloc[i], 2) if pd.notna(bbiboll_l.iloc[i]) else 0,
            "tower": int(tower.iloc[i]) if pd.notna(tower.iloc[i]) else 0,
        },
        "signals": signals,
        "summary": {"buy": buy_count, "sell": sell_count, "hold": hold_count},
        "verdict": verdict,
        "verdict_icon": verdict_icon,
        "buy_reasons": buy_reasons,
        "sell_reasons": sell_reasons,
        "hold_reasons": hold_reasons,
    }

def print_analysis(result):
    if "error" in result:
        print(f"\n 错误: {result['error']}\n")
        return
    r = result["realtime"]
    ind = result["indicators"]
    s = result["summary"]
    sep = "─" * 50
    print(f"\n┌──────────────────────────────────────────────────┐")
    print(f"│  A股量化分析  {r['code']} {r['name']}              │")
    print(f"│  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                    │")
    print(f"└──────────────────────────────────────────────────┘")
    pct_str = f"+{r['pct']}%" if r['pct'] >= 0 else f"{r['pct']}%"
    change_str = f"+{r['change']}" if r['change'] >= 0 else f"{r['change']}"
    print(f"\n 实时行情")
    print(f"  现价: {r['price']:.2f}  |  涨跌: {change_str} ({pct_str})")
    print(f"  开: {r['open']:.2f}  高: {r['high']:.2f}  低: {r['low']:.2f}  昨收: {r['yclose']:.2f}")
    print(f"  成交量: {r['volume']}")
    print(f"\n 技术指标")
    macd_trend = "↑" if ind["macd_bar"] > 0 else "↓"
    macd_status = ""
    if ind["macd_diff"] > 0 and ind["macd_dea"] > 0:
        macd_status = "零上金叉" if ind["macd_diff"] > ind["macd_dea"] else "零上"
    elif ind["macd_diff"] < 0 and ind["macd_dea"] < 0:
        macd_status = "零下" if ind["macd_diff"] < ind["macd_dea"] else "零下金叉"
    print(f"  MACD: DIFF {ind['macd_diff']:.3f}  DEA {ind['macd_dea']:.3f}  红柱 {macd_trend}  {macd_status}")
    kdj_status = "超买" if ind["k"] > 80 else ("超卖" if ind["k"] < 20 else "中位")
    print(f"  KDJ:  K {ind['k']:.1f}  D {ind['d']:.1f}  J {ind['j']:.1f}  ({kdj_status})")
    boll_pos = "上轨" if r['price'] >= ind["boll_u"] else ("中轨" if r['price'] >= ind["boll_m"] else "下轨")
    print(f"  BOLL: 上 {ind['boll_u']:.2f}  中 {ind['boll_m']:.2f}  下 {ind['boll_l']:.2f}  ({boll_pos})")
    bbiboll_pos = "上轨" if r['price'] >= ind["bbiboll_u"] else ("中轨" if r['price'] >= ind["bbiboll_m"] else "下轨")
    print(f"  BBIBOLL: 上 {ind['bbiboll_u']:.2f}  中 {ind['bbiboll_m']:.2f}  下 {ind['bbiboll_l']:.2f}  ({bbiboll_pos})")
    ma_status = "多头排列" if r['price'] > ind["ma5"] > ind["ma10"] > ind["ma60"] else "空头排列" if r['price'] < ind["ma5"] < ind["ma10"] < ind["ma60"] else "交叉"
    print(f"  均线:  MA5 {ind['ma5']:.2f}  MA10 {ind['ma10']:.2f}  MA60 {ind['ma60']:.2f}  ({ma_status})")
    print(f"  DMI:  PDI {ind['pdi']:.1f}  MDI {ind['mdi']:.1f}  ADX {ind['adx']:.1f}")
    tower_txt = "红" if ind["tower"] > 0 else ("绿" if ind["tower"] < 0 else "平")
    print(f"  PSY:  {ind['psy']:.0f}  BIAS: {ind['bias']:.1f}%  SAR: {ind['sar']:.2f}  宝塔: {tower_txt}")
    total = s['buy'] + s['sell'] + s['hold']
    print(f"\n 策略信号 ({total}/19)")
    print(f"  买入: {s['buy']}  |  卖出: {s['sell']}  |  观望: {s['hold']}")
    if result["buy_reasons"]:
        print(f"\n 触发买入 ({len(result['buy_reasons'])})")
        for sig in result["buy_reasons"]:
            print(f"    [{sig['name']}] {sig['reason']}")
    if result["sell_reasons"]:
        print(f"\n 触发卖出 ({len(result['sell_reasons'])})")
        for sig in result["sell_reasons"]:
            print(f"    [{sig['name']}] {sig['reason']}")
    if result["hold_reasons"]:
        print(f"\n 观望信号 ({len(result['hold_reasons'])})")
        for sig in result["hold_reasons"]:
            print(f"    [{sig['name']}] {sig['reason']}")
    print(f"\n 综合建议: {result['verdict']} {result['verdict_icon']}")
    if result["verdict"] == "买入":
        ma5 = ind["ma5"]
        print(f"  核心逻辑: MACD零上金叉 + 均线多头排列 + BOLL中轨上方")
        print(f"  {s['buy']}个策略看多 vs {s['sell']}个看空，多头占优")
        print(f"  建议买入，止损位设在MA5={ma5:.2f}，跌破离场")
    elif result["verdict"] == "卖出":
        print(f"  {s['sell']}个策略看空 vs {s['buy']}个看多，空头占优")
        print(f"  建议卖出/观望，等待企稳再入场")
    else:
        print(f"  多空信号接近，暂时观望")
    print()

def main():
    if len(sys.argv) < 2:
        print("用法: uv run analyze.py <股票代码1> [股票代码2] ...")
        print("示例: uv run analyze.py 600789")
        print("       uv run analyze.py 600789 000001 600519")
        sys.exit(1)
    codes = sys.argv[1:]
    for code in codes:
        code = code.strip()
        print(f"\n正在获取 {code} 数据...")
        result = analyze_stock(code)
        print_analysis(result)

if __name__ == "__main__":
    main()