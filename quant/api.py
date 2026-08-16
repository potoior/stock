import json
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import strategy_engine as se
from data_fetcher import fetch_realtime

app = FastAPI(title="A股量化监控系统")
WEB_DIR = Path(__file__).parent / "web"
DB_PATH = Path(__file__).parent / "stock_cache.db"

app.mount("/lib", StaticFiles(directory=str(WEB_DIR / "lib")), name="lib")

DEFAULT_CODES = ["600789", "000001", "600519", "601318", "000333", "002415"]


def compute_signals(code, current_price=None):
    try:
        df = se.get_daily_data(code)
        if len(df) < 60:
            return {}
        df = df.sort_values("date").reset_index(drop=True)
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)

        if current_price is not None:
            close = pd.concat([close[:-1], pd.Series([current_price])], ignore_index=True)
            high = pd.concat([high[:-1], pd.Series([max(high.iloc[-1], current_price)])], ignore_index=True)
            low = pd.concat([low[:-1], pd.Series([min(low.iloc[-1], current_price)])], ignore_index=True)

        price = current_price if current_price is not None else close.iloc[-1]
        ma5 = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_val = 2 * (dif.iloc[-1] - dea.iloc[-1])
        low9 = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        k_val = k.iloc[-1]
        d_val = d.iloc[-1]
        j_val = 3 * k_val - 2 * d_val
        return {
            "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
            "macd": round(macd_val, 3),
            "macd_bull": bool(dif.iloc[-1] > dea.iloc[-1]),
            "k": round(k_val, 1), "d": round(d_val, 1), "j": round(j_val, 1),
            "kdj_signal": "超卖" if k_val < 20 else ("超买" if k_val > 80 else "中性"),
        }
    except Exception:
        return {}


@app.get("/api/quotes")
def get_quotes(codes: str = ""):
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else DEFAULT_CODES
    quotes = fetch_realtime(code_list)
    result = []
    for q in quotes:
        sig = compute_signals(q["code"], q["price"])
        result.append({
            "code": q["code"], "name": q["name"],
            "price": q["price"], "change": q["change"], "pct": q["pct"],
            "high": q["high"], "low": q["low"], "open": q["open"],
            "volume": q["volume"],
            "ma5": sig.get("ma5"), "ma10": sig.get("ma10"), "ma20": sig.get("ma20"),
            "macd": sig.get("macd"), "macd_bull": sig.get("macd_bull"),
            "k": sig.get("k"), "d": sig.get("d"), "j": sig.get("j"), "kdj_signal": sig.get("kdj_signal"),
        })
    return {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "data": result}


@app.get("/api/kline/{code}")
def get_kline(code: str, days: int = 120):
    df = se.get_daily_data(code)
    df = df.tail(days)
    points = []
    for _, row in df.iterrows():
        points.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            "open": float(row["open"]), "close": float(row["close"]),
            "high": float(row["high"]), "low": float(row["low"]),
            "volume": float(row["volume"]),
        })
    return {"code": code, "data": points}


# ---------------- 自选股 ----------------

@app.get("/api/watchlist")
def get_watchlist():
    wl = se.get_watchlist()
    codes = [it["code"] for it in wl]
    names = {it["code"]: it.get("name", "") for it in wl}
    quotes = fetch_realtime(codes)
    quoted = {q["code"]: q for q in quotes}
    data = []
    for code in codes:
        q = quoted.get(code, {})
        sig = compute_signals(code, q.get("price")) if q else {}
        data.append({
            "code": code,
            "name": names.get(code) or q.get("name", code),
            "price": q.get("price"),
            "pct": q.get("pct"),
            "ma5": sig.get("ma5"),
            "macd_bull": sig.get("macd_bull"),
            "kdj_signal": sig.get("kdj_signal"),
        })
    return {"data": data}


@app.post("/api/watchlist")
async def add_watch(code: str):
    code = code.strip()
    if not code:
        return {"ok": False, "msg": "代码不能为空"}
    rt = se.fetch_realtime([code])
    name = rt[0]["name"] if rt else ""
    ok = se.add_watch(code, name)
    return {"ok": True, "msg": "已添加" if ok else "已在自选中", "name": name}


@app.delete("/api/watchlist/{code}")
def remove_watch(code: str):
    ok = se.remove_watch(code)
    return {"ok": ok, "msg": "已删除" if ok else "未找到"}


# ---------------- 策略管理 ----------------
# params: 每个参数 {default, label, hint}
# detail: 策略详细说明（用于展示给用户）
BUILTIN_METADATA = [
    {"id": "macd", "name": "MACD金叉死叉",
     "params": {"fast": {"default": 12, "label": "快线周期", "hint": "EMA快线天数"}, "slow": {"default": 26, "label": "慢线周期", "hint": "EMA慢线天数"}, "signal": {"default": 9, "label": "信号周期", "hint": "DEA信号线天数"}},
     "detail": """【MACD金叉死叉】
原理：DIFF(EMA12-EMA26)与DEA(EMA9)的交叉判断多空转换。
- 零上金叉（DIFF上穿DEA且都在0轴上方）：锦上添花，强势买入
- 零下金叉：底部反弹信号，若20日内多次零下金叉可靠性更高
- 零下死叉：继续下跌，卖出
- 零上死叉：强势行情中的回调，不追空，观望
适合趋势中段操作，零下多次金叉信号可靠性较高。"""},
    {"id": "kdj", "name": "KDJ超买超卖",
     "params": {"n": {"default": 9, "label": "统计周期", "hint": "RSV统计天数"}, "k1": {"default": 3, "label": "K平滑", "hint": "K线平滑参数"}, "d1": {"default": 3, "label": "D平滑", "hint": "D线平滑参数"}},
     "detail": """【KDJ超买超卖】
原理：随机指标，K/D/J三线在0-100间波动。
- 超卖区（K<20, D<30）：价格被过度打压，反弹概率大，买入
- 超买区（K>80, D>70）：获利盘多，回调风险高，卖出
- K上穿D=金叉，K下穿D=死叉，中位金叉/死叉参考性递减
适合震荡行情的高抛低吸，趋势行情中易钝化失效。"""},
    {"id": "ma_stop", "name": "均线止损",
     "params": {"period": {"default": 5, "label": "均线周期", "hint": "止损均线天数，默认5日"}},
     "detail": """【均线止损】
原理：价格与短期均线的相对位置决定持仓/离场。
- 价格站上MA周期线：趋势向上，持有/买入
- 价格跌破MA周期线：止损离场，保护本金
周期可调：短线用MA5，中线可用MA10/MA20。
是最简单有效的资金保护策略，与任何买入策略搭配使用。"""},
    {"id": "boll", "name": "BOLL布林线",
     "params": {"period": {"default": 20, "label": "周期", "hint": "均线周期，默认20"}, "std": {"default": 2, "label": "标准差倍数", "hint": "带宽宽度，默认2"}},
     "detail": """【BOLL布林线】
原理：中轨=MA(周期)，上下轨=中轨±标准差×倍数。
- 价格触及下轨：超跌反弹，买入
- 价格触及上轨：超涨回调，卖出
- 价格在中轨上方趋势可操作，中轨下方不建议操作
- 带宽收窄后变宽=即将变盘，带宽持续扩大=趋势运行中
适合判断极端行情与趋势位置。"""},
    {"id": "dmi", "name": "DMI趋势",
     "params": {"n": {"default": 14, "label": "统计周期", "hint": "DMI统计天数，默认14"}, "m": {"default": 6, "label": "平滑周期", "hint": "ADX平滑天数，默认6"}},
     "detail": """【DMI趋势】
原理：PDI/MDI衡量多空力量，ADX衡量趋势强度。
- ADX<20：无明确趋势，盘整，观望
- ADX>40且PDI>MDI：强多头，买入
- ADX>40且PDI<MDI：强空头，卖出
- ADX介于20-40之间：按PDI vs MDI给方向
适合判断是否处于趋势中以及趋势方向。"""},
    {"id": "psy", "name": "PSY心理线",
     "params": {"period": {"default": 12, "label": "统计周期", "hint": "统计天数，默认12"}},
     "detail": """【PSY心理线】
原理：统计N日内上涨天数占比，反映市场心理。
- PSY≤25：市场极度悲观，超卖，有望反弹，买入
- PSY≥75：市场极度乐观，超买，获利盘多，卖出
- 25-75之间：正常区间，观望
是逆向指标，极端位置使用效果最佳。"""},
    {"id": "bias", "name": "BIAS乖离率",
     "params": {"short": {"default": 3, "label": "短线阈值%", "hint": "BIAS6超买超卖阈值"}, "long": {"default": 5, "label": "中长线阈值%", "hint": "BIAS12超买超卖阈值"}},
     "detail": """【BIAS乖离率】
原理：价格偏离均线的百分比(6/12/24日三线)，衡量偏离程度。
- BIAS6≤-阈值 or BIAS12≤-长阈值：超跌，反弹，买入
- BIAS6≥+阈值 or BIAS12≥+长阈值：超涨，回调，卖出
乖离越大回调/反弹概率越高，适合逆向操作。"""},
    {"id": "sar", "name": "SAR止损",
     "params": {"af_init": {"default": 0.02, "label": "初始加速因子", "hint": "默认0.02"}, "af_max": {"default": 0.2, "label": "最大加速因子", "hint": "默认0.2"}},
     "detail": """【SAR止损】(抛物线指标)
原理：从关键价格出发，随趋势加速追踪的止盈止损线。
- 价格在SAR线上方：翻红，趋势向上，持有/买入
- 价格跌破SAR线：翻绿，趋势转坏，卖出
具有自动跟踪止盈功能，牛市中回撤时能锁定利润。"""},
    {"id": "bbiboll", "name": "BBIBOLL多空布林",
     "params": {},
     "detail": """【BBIBOLL多空布林】
原理：BBI=(MA3+MA6+MA12+MA24)/4为多空分界线，上下轨=BBI±6倍11日标准差。
- 价格突破上轨：大概率回调，卖出
- 价格跌破下轨：大概率反弹，买入
- 价格在中轨上方：多方强势，可操作
- 价格在中轨下方：空方强势，观望
结合了多周期均线与波动率，趋势参考性强。"""},
    {"id": "tower", "name": "宝塔线TOWER",
     "params": {},
     "detail": """【宝塔线TOWER】
原理：以收盘价相对前一根高/低价的突破判断转势。
- 收盘价突破前一根最高价：翻红，买入/持有
- 收盘价跌破前一根最低价：翻绿，卖出
- 未突破未跌破：延续前态
可配合红绿柱观察趋势延续与反转。"""},
    {"id": "ma_combo", "name": "均线组合",
     "params": {"short": {"default": 5, "label": "短期均线", "hint": "默认5"}, "mid": {"default": 10, "label": "中期均线", "hint": "默认10"}, "long": {"default": 60, "label": "长期均线", "hint": "默认60"}},
     "detail": """【均线组合】(5/10/60)
原理：三条均线的排列形态判断趋势。
- 价格>短>中>长：完美多头排列，趋势向上，买入
- 价格跌破短或中期均线：短期走坏，卖出
适合判断中长期多空格局，多头排列是最强持股理由。"""},
    {"id": "two_line", "name": "二线法",
     "params": {"short": {"default": 5, "label": "短期均线", "hint": "默认MA5"}, "long": {"default": 10, "label": "长期均线", "hint": "默认MA10，可改MA13/MA20"}},
     "detail": """【二线法】
原理：两条均线的相对位置判断短线可操作性。
- 短均线>长均线：短线趋势向上，可操作，买入
- 短均线<长均线：短线走弱，清仓观望
两条线的周期均可自定义，如MA5/MA13、MA5/MA20，
也可用MA10/MA20做更长周期的二线判断。"""},
    {"id": "life_line", "name": "生命线",
     "params": {"period": {"default": 60, "label": "生命线周期", "hint": "默认60日，可改120日"}},
     "detail": """【生命线】
原理：中长期均线(默认60日)作为牛熊分界线。
- 价格在生命线上方：中期多头市场，积极做多
- 价格在生命线下方：中期空头市场，空仓观望
60日线是A股公认的生命线，跌破后中期趋势转弱，
也可用120日/年线做更长周期判断。"""},
    {"id": "three_third", "name": "三分法",
     "params": {"p1": {"default": 7, "label": "第一分线", "hint": "默认MA7"}, "p2": {"default": 13, "label": "第二分线", "hint": "默认MA13"}, "p3": {"default": 20, "label": "第三分线", "hint": "默认MA20"}},
     "detail": """【三分法】(7/13/20)
原理：把资金分三份，沿均线系统分批建仓/减仓。
- 价格同时站上三线：趋势确立，分三批建仓，买入
- 跌破第一线(7日)：先减一份仓
- 跌破更多线：继续减仓
通过分批操作降低单次决策风险。"""},
    {"id": "sparrow", "name": "麻雀战术",
     "params": {"lookback": {"default": 5, "label": "回看周期", "hint": "默认5日"}, "target": {"default": 2.5, "label": "止盈目标%", "hint": "默认2.5%"}},
     "detail": """【麻雀战术】
原理：短线快进快出，赚到目标即走。
- 自回看周期内低点已有target%涨幅：已达到止盈目标，见好就收，卖出
- 未达目标：继续持有观察
适合震荡市中做小波段，纪律性止盈，不贪不恋。"""},
    {"id": "bounce", "name": "反弹量化",
     "params": {"rebound_pct": {"default": 0.5, "label": "反弹幅度×昨日跌幅", "hint": "默认0.5(50%)"}, "vol_increase": {"default": 20, "label": "放量阈值%", "hint": "成交量增幅需超此值"}},
     "detail": """【反弹量化】
原理：昨日大跌后今日反弹是否有效，需放量确认。
- 昨日下跌，今日涨幅>昨日跌幅×50%，且成交量放量>阈值%：反弹有效，买入
- 反弹幅度不足或未放量：反弹不可靠，观望
判断超跌反弹的可靠性，放量是关键确认信号。"""},
    {"id": "volume_div", "name": "量价背离",
     "params": {"lookback": {"default": 10, "label": "回看周期", "hint": "默认10日"}, "shrink": {"default": 0.7, "label": "缩量阈值", "hint": "量<均量×此值视为缩量"}, "expand": {"default": 1.3, "label": "放量阈值", "hint": "量>均量×此值视为放量"}},
     "detail": """【量价背离】
原理：价格创新高时量的配合程度判断上涨真实性。
- 创回看周期新高但量萎缩(缩量阈值)：无量上涨，主力可能出货，卖出
- 创回看周期新高且放量(放量阈值)：量价配合，健康上涨，买入
判断上涨的含金量，放量突破比无量上涨更可靠。"""},
    {"id": "resonance", "name": "三指标共振",
     "params": {},
     "detail": """【三指标共振】
原理：MACD、KDJ、BOLL、MA5四个指标同一天共振向上才是可靠买点。
- 同一交易日：MACD金叉 + KDJ金叉 + 价格在BOLL中轨上方 + 价格站上MA5
- 四者共振：多方力量形成合力，买入信号可靠
- 未共振：力量不统一，观望
最严格的多头确认信号，宁可错过不可做错。"""},
    {"id": "dmi_psy", "name": "DMI+PSY超跌",
     "params": {"pdi_threshold": {"default": 5, "label": "PDI下限", "hint": "PDI低于此值视为极端超跌"}, "psy_threshold": {"default": 25, "label": "PSY上限", "hint": "PSY不高于此值"}},
     "detail": """【DMI+PSY超跌】
原理：两个独立指标同时出现极端超跌才是底部信号。
- PDI<5（多头力量极弱）+ PSY≤25（心理极度悲观）
- 双指标共振：极致超跌后的反弹概率极大，买入
极少出现的极限信号，出现时是较好的抢反弹时机。"""},
]


@app.get("/api/strategies")
def get_strategies():
    saved = {s["id"]: s for s in se.get_strategies()}
    result = []
    for meta in BUILTIN_METADATA:
        saved_item = saved.get(meta["id"], {})
        saved_params = saved_item.get("params", {})
        param_defs = {}
        clean_params = {}
        for key, spec in meta["params"].items():
            val = float(saved_params.get(key, spec["default"]))
            param_defs[key] = {"value": val, "label": spec["label"], "hint": spec["hint"]}
            clean_params[key] = val
        result.append({
            "id": meta["id"], "name": meta["name"],
            "type": "builtin", "builtin": True,
            "enabled": saved_item.get("enabled", True),
            "params": clean_params,
            "param_defs": param_defs,
            "detail": meta["detail"],
        })
    for s in se.get_strategies():
        if s.get("type") == "custom":
            s = {**s, "builtin": False}
            result.append(s)
    return {"strategies": result}


@app.post("/api/strategies")
async def create_strategy(body: dict):
    strategies = se.get_strategies()
    sid = "custom_" + str(len([s for s in strategies if s.get("type") == "custom"]) + 1)
    strategy = {
        "id": sid,
        "name": body.get("name") or "自定义策略" + sid,
        "type": "custom",
        "enabled": body.get("enabled", True),
        "buy_rule": body.get("buy_rule", "") or "",
        "sell_rule": body.get("sell_rule", "") or "",
        "buy": body.get("buy", []),
        "sell": body.get("sell", []),
    }
    strategies.append(strategy)
    se.save_strategies(strategies)
    se.clear_ai_cache()
    return {"ok": True, "strategy": strategy}


@app.put("/api/strategies/{sid}")
async def update_strategy(sid: str, req: dict):
    strategies = se.get_strategies()
    found = False
    if sid in [m["id"] for m in BUILTIN_METADATA]:
        # 内置策略：只保存 enabled 与 params，忽略 schema 展示字段
        clean = {
            "id": sid,
            "enabled": bool(req.get("enabled", True)),
            "params": req.get("params", {}),
        }
        for s in strategies:
            if s["id"] == sid:
                s.update(clean)
                found = True
                break
        if not found:
            strategies.append({"id": sid, "type": "builtin", **clean})
            found = True
    else:
        for s in strategies:
            if s["id"] == sid:
                s.update(req)
                found = True
                break
    se.save_strategies(strategies)
    if found:
        se.clear_ai_cache()
    return {"ok": found}


@app.delete("/api/strategies/{sid}")
def delete_strategy(sid: str):
    strategies = se.get_strategies()
    new = [s for s in strategies if s["id"] != sid]
    changed = len(new) != len(strategies)
    se.save_strategies(new)
    if changed:
        se.clear_ai_cache()
    return {"ok": changed, "msg": "已删除" if changed else "未找到"}


@app.get("/api/strategy-metrics")
def strategy_metrics():
    return {"metrics": se.CONDITION_METRIC_META}


# ---------------- 分析 ----------------

@app.get("/api/analyze/{code}")
def analyze(code: str, ai: int = 1):
    return se.analyze(code, use_ai=bool(ai))


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)