"""股票财务数据获取(东方财富双接口组合)。

数据来源:
- push2.eastmoney.com/api/qt/stock/get: 实时 PE/PB/市值/52周高低
- emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew: 财报数据

缓存:sqlite 1 天有效(财报不变,PE/市值每日变)
"""

import json
import logging
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("stock_finance")

ENGINE_HOME = Path(__file__).parent
DB_PATH = ENGINE_HOME / "stock_cache.db"
CACHE_TTL = 86400  # 1 天


def _eastmoney_secid(code: str) -> str:
    """6位代码 → 东财 secid(1.600xxx / 0.000xxx / 0.300xxx / 1.688xxx)。"""
    if code.startswith(("688", "60", "11", "13")):
        return f"1.{code}"
    return f"0.{code}"


def _eastmoney_secucode(code: str) -> str:
    """6位代码 → 东财 SECUCODE(600519.SH / 000001.SZ)。"""
    if code.startswith(("688", "60", "11", "13")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _fetch_realtime_finance(code: str) -> dict:
    """从 push2 接口取实时 PE/PB/市值/52周高低。

    字段(f162/f163/f164 都是 ×100 整数,需 /100):
    - f57=code, f58=name
    - f162=PE(动), f163=PE(静), f164=PE(TTM), f167=PB
    - f116=总市值, f117=流通市值, f84=总股本, f85=流通股本, f92=每股净资产
    - f191=52周高(原始值负号?需处理), f192=52周低
    """
    secid = _eastmoney_secid(code)
    # 注意:f191/f192 在东财里可能要换其他字段,先用 f173 / f170 试
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
        "&fields=f57,f58,f84,f85,f92,f116,f117,f162,f163,f164,f167"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        d = data.get("data") or {}
        return {
            "code": d.get("f57", code),
            "name": d.get("f58", ""),
            "total_share": d.get("f84"),  # 总股本(股)
            "float_share": d.get("f85"),  # 流通股本(股)
            "bps": d.get("f92"),  # 每股净资产
            "total_mv": d.get("f116"),  # 总市值(元)
            "float_mv": d.get("f117"),  # 流通市值(元)
            "pe_dynamic": (d.get("f162") or 0) / 100,  # PE 动态
            "pe_static": (d.get("f163") or 0) / 100,  # PE 静态
            "pe_ttm": (d.get("f164") or 0) / 100,  # PE TTM
            "pb": (d.get("f167") or 0) / 100,  # PB
        }
    except Exception as e:
        log.warning("实时财务数据获取失败 %s: %s", code, e)
        return {}


def _fetch_report_finance(code: str) -> dict:
    """从 F10 接口取最新一期财报数据(ROE/毛利率/净利率/EPS/营收/净利润/同比)。

    返回最近一期已披露的财报数据,字段:
    - report_name: 报告期(如 "2026中报")
    - eps: 每股收益
    - bps: 每股净资产
    - roe: ROE(%)
    - gross_margin: 毛利率(%)
    - net_margin: 净利率(%)
    - revenue: 营业总收入(元)
    - net_profit: 归母净利润(元)
    - revenue_yoy: 营收同比(%)
    - profit_yoy: 净利润同比(%)
    - debt_ratio: 资产负债率(%)
    """
    secucode = _eastmoney_secucode(code)
    url = (
        f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
        f"ZYZBAjaxNew?type=0&code={secucode}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://emweb.securities.eastmoney.com/",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode("utf-8"))
        records = data.get("data") or []
        if not records:
            return {}
        # records 按报告期倒序,第一条是最新的
        r = records[0]
        return {
            "report_name": r.get("REPORT_DATE_NAME", ""),
            "report_date": r.get("REPORT_DATE", ""),
            "eps": r.get("EPSJB"),
            "bps": r.get("BPS"),
            "roe": r.get("ROEJQ"),
            "gross_margin": r.get("XSMLL"),
            "net_margin": r.get("XSJLL"),
            "revenue": r.get("TOTALOPERATEREVE"),
            "net_profit": r.get("PARENTNETPROFIT"),
            "revenue_yoy": r.get("TOTALOPERATEREVETZ"),
            "profit_yoy": r.get("PARENTNETPROFITTZ"),
            "debt_ratio": r.get("ZCFZL"),
        }
    except Exception as e:
        log.warning("财报数据获取失败 %s: %s", code, e)
        return {}


def _cache_get(code: str) -> dict | None:
    """从 sqlite 读缓存,过期返 None。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.execute(
            "SELECT data_json, ts FROM stock_finance WHERE code=?",
            (code,),
        )
        row = cur.fetchone()
        conn.close()
        if row and row[1] and int(time.time()) - row[1] < CACHE_TTL:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _cache_put(code: str, data: dict) -> None:
    """写缓存。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_finance (
                code TEXT PRIMARY KEY,
                data_json TEXT,
                ts INTEGER
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO stock_finance(code, data_json, ts) VALUES(?,?,?)",
            (code, json.dumps(data, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("财务数据缓存写入失败 %s: %s", code, e)


def fetch_finance(code: str) -> dict:
    """获取股票财务数据(实时 + 财报组合),sqlite 缓存 1 天。

    Args:
        code: 6 位 A 股代码
    Returns:
        dict: {
            code, name,
            pe_dynamic, pe_static, pe_ttm, pb,
            total_mv, float_mv, total_share, float_share, bps,
            report_name, report_date, eps, roe, gross_margin, net_margin,
            revenue, net_profit, revenue_yoy, profit_yoy, debt_ratio,
            ts
        }
    """
    code = (code or "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        return {"error": f"代码必须是 6 位数字,实际 '{code}'"}

    cached = _cache_get(code)
    if cached:
        return cached

    rt = _fetch_realtime_finance(code)
    rep = _fetch_report_finance(code)
    if not rt and not rep:
        return {"error": f"获取 {code} 财务数据失败(可能代码错或接口异常)"}

    out = {**rt, **rep, "code": code, "ts": int(time.time())}
    _cache_put(code, out)
    return out


def fmt_finance(data: dict) -> str:
    """格式化财务数据为 markdown 文本(LLM 友好)。"""
    if "error" in data:
        return f"❌ {data['error']}"

    def _yi(v):
        """元 → 亿元(保留 2 位)。"""
        if v is None:
            return "-"
        return f"{v / 1e8:.2f}"

    def _pct(v):
        """数值 → 百分比(已 ×100,保留 2 位)。"""
        if v is None:
            return "-"
        return f"{v:.2f}%"

    def _num(v, prec=2):
        """数值格式化,None 显示 '-'。"""
        if v is None:
            return "-"
        try:
            return f"{float(v):.{prec}f}"
        except (TypeError, ValueError):
            return str(v)

    lines = [
        f"### {data.get('name', '-') or '-'}({data.get('code', '-')}) 财务数据",
        "",
        "**实时估值**",
        f"- PE(动/静/TTM): {_num(data.get('pe_dynamic'))} / {_num(data.get('pe_static'))} / {_num(data.get('pe_ttm'))}",
        f"- PB: {_num(data.get('pb'))}",
        f"- 总市值: {_yi(data.get('total_mv'))} 亿",
        f"- 流通市值: {_yi(data.get('float_mv'))} 亿",
        f"- 每股净资产: {_num(data.get('bps'))}",
        "",
        f"**财报数据**({data.get('report_name', '-')})",
        f"- EPS: {_num(data.get('eps'))}",
        f"- ROE: {_pct(data.get('roe'))}",
        f"- 毛利率: {_pct(data.get('gross_margin'))}",
        f"- 净利率: {_pct(data.get('net_margin'))}",
        f"- 营业收入: {_yi(data.get('revenue'))} 亿(同比 {_pct(data.get('revenue_yoy'))})",
        f"- 归母净利润: {_yi(data.get('net_profit'))} 亿(同比 {_pct(data.get('profit_yoy'))})",
        f"- 资产负债率: {_pct(data.get('debt_ratio'))}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    d = fetch_finance(code)
    print(json.dumps(d, indent=2, ensure_ascii=False))
    print()
    print(fmt_finance(d))
