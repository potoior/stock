import json
import sqlite3
import pandas as pd
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import strategy_engine as se
from data_fetcher import get_daily_data, fetch_realtime

app = FastAPI(title="A股量化监控系统")
WEB_DIR = Path(__file__).parent / "web"
DB_PATH = Path(__file__).parent / "stock_cache.db"

app.mount("/lib", StaticFiles(directory=str(WEB_DIR / "lib")), name="lib")

DEFAULT_CODES = ["600789", "000001", "600519", "601318", "000333", "002415"]


def compute_signals(code, current_price=None):
    try:
        df = get_daily_data(code, "20240101")
        if len(df) < 60:
            return {}
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

BUILTIN_METADATA = [
    {"id": "macd", "name": "MACD金叉死叉", "desc": "零上金叉/零下死叉/零下多重金叉", "params": {"fast": 12, "slow": 26, "signal": 9}},
    {"id": "kdj", "name": "KDJ超买超卖", "desc": "超卖低价区买入/超买高价区卖出", "params": {"n": 9, "k1": 3, "d1": 3}},
    {"id": "ma_stop", "name": "5日均线止损", "desc": "站上5日线做多/跌破止损", "params": {}},
    {"id": "boll", "name": "BOLL布林线", "desc": "下轨买入/上轨卖出/中轨定方向", "params": {"period": 20, "std": 2}},
    {"id": "dmi", "name": "DMI趋势", "desc": "PDI与MDI方向对比+ADX趋势强度", "params": {"n": 14, "m": 6}},
    {"id": "psy", "name": "PSY心理线", "desc": "超卖(≤25)买入/超买(≥75)卖出", "params": {"period": 12}},
    {"id": "bias", "name": "BIAS乖离率", "desc": "超跌买/超涨卖（6/12/24日三值）", "params": {"short": 3, "long": 5}},
    {"id": "sar", "name": "SAR止损", "desc": "抛物线翻转定买卖", "params": {}},
    {"id": "bbiboll", "name": "BBIBOLL多空布林", "desc": "BBI±6倍11日标准差", "params": {}},
    {"id": "tower", "name": "宝塔线TOWER", "desc": "翻红买/翻绿卖", "params": {}},
    {"id": "ma_combo", "name": "均线组合", "desc": "5>10>60多头排列买", "params": {}},
    {"id": "two_line", "name": "二线法", "desc": "MA5上穿MA10短线做", "params": {}},
    {"id": "life_line", "name": "60日生命线", "desc": "价格站上MA60做多", "params": {}},
    {"id": "three_third", "name": "三分法", "desc": "7/13/20日线，分批建仓", "params": {}},
    {"id": "sparrow", "name": "麻雀战术", "desc": "赚2.5%即走，快进快出", "params": {}},
    {"id": "bounce", "name": "反弹量化", "desc": "反弹超昨日跌幅50%+放量20%", "params": {}},
    {"id": "volume_div", "name": "量价背离", "desc": "价新高量萎缩卖出", "params": {}},
    {"id": "resonance", "name": "三指标共振", "desc": "MACD+KDJ+BOLL+MA5同向", "params": {}},
    {"id": "dmi_psy", "name": "DMI+PSY超跌", "desc": "PDI<5且PSY≤25超跌买", "params": {}},
]


@app.get("/api/strategies")
def get_strategies():
    saved = {s["id"]: s for s in se.get_strategies()}
    result = []
    for meta in BUILTIN_METADATA:
        saved_item = saved.get(meta["id"], {})
        params = {**meta["params"], **saved_item.get("params", {})}
        result.append({
            "id": meta["id"], "name": meta["name"], "desc": meta["desc"],
            "type": "builtin", "builtin": True,
            "enabled": saved_item.get("enabled", True),
            "params": params,
        })
    for s in se.get_strategies():
        if s.get("type") == "custom":
            result.append(s)
    return {"strategies": result}


@app.post("/api/strategies")
async def create_strategy(body: dict):
    strategies = se.get_strategies()
    sid = "custom_" + str(len([s for s in strategies if s.get("type") == "custom"]) + 1)
    strat = {
        "id": sid,
        "name": body.get("name") or "自定义策略" + sid,
        "type": "custom",
        "enabled": body.get("enabled", True),
        "buy": body.get("buy", []),
        "sell": body.get("sell", []),
    }
    strategies.append(strat)
    se.save_strategies(strategies)
    return {"ok": True, "strategy": strat}


@app.put("/api/strategies/{sid}")
async def update_strategy(sid: str, req: dict):
    strategies = se.get_strategies()
    found = False
    for s in strategies:
        if s["id"] == sid:
            s.update(req)
            found = True
            break
    if not found and sid in [m["id"] for m in BUILTIN_METADATA]:
        strategies.append({"id": sid, **req})
        found = True
    se.save_strategies(strategies)
    return {"ok": found}


@app.delete("/api/strategies/{sid}")
def delete_strategy(sid: str):
    strategies = se.get_strategies()
    new = [s for s in strategies if s["id"] != sid]
    changed = len(new) != len(strategies)
    se.save_strategies(new)
    return {"ok": changed, "msg": "已删除" if changed else "未找到"}


@app.get("/api/strategy-metrics")
def strategy_metrics():
    return {"metrics": se.CONDITION_METRIC_META}


# ---------------- 分析 ----------------

@app.get("/api/analyze/{code}")
def analyze(code: str):
    return se.analyze(code)


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)