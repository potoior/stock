"""股票市场扩展数据(龙虎榜/北向资金/主力资金流/概念板块/指数行情)。

数据来源:
- datacenter-web.eastmoney.com: 龙虎榜 / 概念板块反查
- push2.eastmoney.com: 主力资金流 / 指数行情
- push2.eastmoney.com/api/qt/kamt.kline: 北向资金(沪深股通额度)

所有接口不缓存(实时数据),失败返回空列表/None 不抛异常。
"""

import json
import logging
import urllib.request

log = logging.getLogger("stock_market_extras")

UA = "Mozilla/5.0"


def _get_json(url, referer=None, timeout=10):
    """通用 GET + JSON 解析,失败返回 None。"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("接口调用失败 %s: %s", url[:80], e)
        return None


def _eastmoney_secid(code: str) -> str:
    """6位代码 → 东财 secid(1.600xxx / 0.000xxx / 0.300xxx / 1.688xxx)。"""
    if code.startswith(("688", "60", "11", "13")):
        return f"1.{code}"
    return f"0.{code}"


# ---------------- 1. 龙虎榜 ----------------


def fetch_lhb(date_str=None, top_n=20):
    """获取龙虎榜数据(当日或指定日期)。

    Args:
        date_str: 日期 'YYYYMMDD' 或 'YYYY-MM-DD' 或 None=最新
        top_n: 返回条数,默认 20

    Returns: [{code, name, date, close, pct, buy_amt, sell_amt, net_amt, explain, deal_ratio}, ...]
             失败返回 []
    """
    url = (
        f"https://datacenter-web.eastmoney.com/api/data/v1/get"
        f"?reportName=RPT_DAILYBILLBOARD_DETAILS&columns=ALL"
        f"&pageSize={top_n}&sortColumns=TRADE_DATE&sortTypes=-1"
    )
    if date_str:
        # 标准化日期
        d = date_str.replace("-", "")
        if d.isdigit() and len(d) == 8:
            d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            url += f"&filter=(TRADE_DATE>='{d_fmt} 00:00:00')(TRADE_DATE<='{d_fmt} 23:59:59')"
    data = _get_json(url, referer="https://data.eastmoney.com/stock/lhb.html")
    if not data or not data.get("success"):
        return []
    rows = (data.get("result") or {}).get("data") or []
    # 按股票代码去重,合并上榜原因
    seen = {}
    for r in rows:
        code = r.get("SECURITY_CODE", "")
        if not code:
            continue
        if code not in seen:
            seen[code] = {
                "code": code,
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "date": (r.get("TRADE_DATE") or "")[:10],
                "close": r.get("CLOSE_PRICE"),
                "pct": r.get("CHANGE_RATE"),
                "buy_amt": r.get("BILLBOARD_BUY_AMT"),
                "sell_amt": r.get("BILLBOARD_SELL_AMT"),
                "net_amt": r.get("BILLBOARD_NET_AMT"),
                "explain": r.get("EXPLAIN", ""),
                "reasons": [r.get("EXPLANATION", "")] if r.get("EXPLANATION") else [],
                "deal_ratio": r.get("DEAL_AMOUNT_RATIO"),
            }
        else:
            if r.get("EXPLANATION") and r["EXPLANATION"] not in seen[code]["reasons"]:
                seen[code]["reasons"].append(r["EXPLANATION"])
    out = list(seen.values())
    # 把 reasons 列表合并为字符串
    for o in out:
        o["reason"] = " / ".join(o.pop("reasons"))
    return out


# ---------------- 2. 北向资金 ----------------


def fetch_north_flow(days=5):
    """获取近 N 日北向资金(沪股通+深股通)净流入数据。

    返回每日北向资金可用额度(总额520亿)+ 当日余额,实际净流入 = 额度 - 余额。

    Returns: [{date, hk2sh_net, hk2sz_net, total_net, sh2hk_net, total}, ...]
             失败返回 []
    """
    url = (
        f"http://push2.eastmoney.com/api/qt/kamt.kline/get"
        f"?fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
        f"&klt=101&lmt={days}"
    )
    data = _get_json(url)
    if not data or not data.get("data"):
        return []
    d = data["data"]
    # hk2sh / hk2sz: [日期, 当日余额, 当日额度, ?]
    hk2sh = d.get("hk2sh") or []
    hk2sz = d.get("hk2sz") or []
    sh2hk = d.get("sh2hk") or []
    out = []
    n = max(len(hk2sh), len(hk2sz))
    for i in range(n):
        item = {"date": "", "hk2sh_net": None, "hk2sz_net": None,
                "total_net": None, "sh2hk_net": None}
        if i < len(hk2sh):
            parts = hk2sh[i].split(",")
            item["date"] = parts[0]
            # 净流入 = 额度 - 余额(单位:元)
            if len(parts) >= 3:
                balance = float(parts[1]) if parts[1] else 0
                quota = float(parts[2]) if parts[2] else 0
                item["hk2sh_net"] = round((quota - balance) / 1e8, 2)  # 转亿元
        if i < len(hk2sz):
            parts = hk2sz[i].split(",")
            if not item["date"]:
                item["date"] = parts[0]
            if len(parts) >= 3:
                balance = float(parts[1]) if parts[1] else 0
                quota = float(parts[2]) if parts[2] else 0
                item["hk2sz_net"] = round((quota - balance) / 1e8, 2)
        if i < len(sh2hk):
            parts = sh2hk[i].split(",")
            if len(parts) >= 3:
                balance = float(parts[1]) if parts[1] else 0
                quota = float(parts[2]) if parts[2] else 0
                item["sh2hk_net"] = round((quota - balance) / 1e8, 2)
        # 合计北向
        if item["hk2sh_net"] is not None and item["hk2sz_net"] is not None:
            item["total_net"] = round(item["hk2sh_net"] + item["hk2sz_net"], 2)
        out.append(item)
    return out


# ---------------- 3. 主力资金流(个股) ----------------


def fetch_main_flow(code):
    """获取个股当日主力资金流(超大单/大单/中单/小单净流入)。

    Returns: {code, name, price, main_net, main_pct, super_large, large, medium, small}
             失败返回 {error: ...}
    """
    if not code or not code.isdigit() or len(code) != 6:
        return {"error": f"代码必须是 6 位数字,实际 '{code}'"}
    secid = _eastmoney_secid(code)
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
        f"&fields=f57,f58,f43,f184,f135,f136,f137,f138"
    )
    data = _get_json(url)
    if not data or not data.get("data"):
        return {"error": f"获取 {code} 资金流失败(接口异常或代码错)"}
    d = data["data"]

    def _yi(v):
        """原始单位是元,转亿元(保留 2 位)。"""
        if v is None:
            return 0.0
        return round(float(v) / 1e8, 2)

    super_large = _yi(d.get("f135"))
    large = _yi(d.get("f136"))
    medium = _yi(d.get("f137"))
    small = _yi(d.get("f138"))
    # 主力 = 超大单 + 大单(避免 f62 单位不一致,直接算)
    main_net = round(super_large + large, 2)
    # f43 现价需 /100(东财返回的是×100 整数)
    price = d.get("f43")
    if price and isinstance(price, (int, float)) and price > 1000:
        price = round(float(price) / 100, 2)
    # f184 主力净占比已 ×100,直接用
    main_pct = d.get("f184")
    if main_pct and isinstance(main_pct, (int, float)):
        main_pct = round(float(main_pct), 2)
    return {
        "code": d.get("f57", code),
        "name": d.get("f58", ""),
        "price": price,
        "main_net": main_net,
        "main_pct": main_pct,
        "super_large": super_large,
        "large": large,
        "medium": medium,
        "small": small,
    }


# ---------------- 4. 概念板块反查 ----------------


def fetch_concept_sectors(code):
    """给定股票代码,反查它属于哪些行业/板块。

    Returns: [{board_name, board_code, board_type}, ...]
             board_type: 'industry'(行业) 或 'concept'(概念)
             失败返回 []
    """
    if not code or not code.isdigit() or len(code) != 6:
        return []
    # 用 RPT_LICO_FN_CPD 接口(财务报告里有 BOARD_NAME/BOARD_CODE)
    url = (
        f"https://datacenter-web.eastmoney.com/api/data/v1/get"
        f"?reportName=RPT_LICO_FN_CPD&columns=BOARD_NAME,BOARD_CODE,SECURITY_NAME_ABBR"
        f"&filter=(SECURITY_CODE=%22{code}%22)&pageSize=20"
    )
    data = _get_json(url, referer="https://data.eastmoney.com/")
    if not data or not data.get("success"):
        return []
    rows = (data.get("result") or {}).get("data") or []
    seen = set()
    out = []
    for r in rows:
        bk = r.get("BOARD_CODE", "")
        name = r.get("BOARD_NAME", "")
        if bk and bk not in seen:
            seen.add(bk)
            out.append({
                "board_name": name,
                "board_code": bk,
                "board_type": "industry",  # RPT_LICO_FN_CPD 主要是行业板块
            })
    return out


# ---------------- 5. 指数行情 ----------------


# 主要指数 secid 映射
INDEX_MAP = {
    "上证指数": "1.000001",
    "上证": "1.000001",
    "深证成指": "0.399001",
    "深成": "0.399001",
    "深成指": "0.399001",
    "创业板指": "0.399006",
    "创业板": "0.399006",
    "科创50": "1.000688",
    "科创板": "1.000688",
    "北证50": "0.899050",
    "北证": "0.899050",
    "沪深300": "1.000300",
    "中证500": "1.000905",
    "中证1000": "1.000852",
}


def fetch_index(name=None):
    """获取指数实时行情。

    Args:
        name: 指数名(如 '上证'/'深成'/'创业板'/'科创50'/'北证50'),None=全部主要指数

    Returns: 单个指数 dict 或 列表[{name, code, price, pct, change}, ...]
    """
    if name:
        secid = INDEX_MAP.get(name)
        if not secid:
            # 模糊匹配
            for k, v in INDEX_MAP.items():
                if name in k or k in name:
                    secid = v
                    name = k
                    break
        if not secid:
            return {"error": f"未知指数: {name},支持: {', '.join(INDEX_MAP.keys())}"}
        return _fetch_one_index(name, secid)
    # 全部主要指数
    main_indices = [
        ("上证指数", "1.000001"),
        ("深证成指", "0.399001"),
        ("创业板指", "0.399006"),
        ("科创50", "1.000688"),
        ("北证50", "0.899050"),
    ]
    out = []
    for nm, sid in main_indices:
        r = _fetch_one_index(nm, sid)
        if "error" not in r:
            out.append(r)
    return out


def _fetch_one_index(name, secid):
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
        f"&fields=f57,f58,f43,f170,f169"
    )
    data = _get_json(url)
    if not data or not data.get("data"):
        return {"error": f"获取 {name} 失败"}
    d = data["data"]
    # f43 价格 / f170 涨跌幅(%) / f169 涨跌额 — 东财指数接口都是 ×100 整数
    price = d.get("f43")
    pct = d.get("f170")
    change = d.get("f169")
    if price and isinstance(price, (int, float)):
        price = round(float(price) / 100, 2)
    if change and isinstance(change, (int, float)):
        change = round(float(change) / 100, 2)
    if pct and isinstance(pct, (int, float)):
        pct = round(float(pct) / 100, 2)
    return {
        "name": name,
        "code": d.get("f57", ""),
        "price": price,
        "pct": pct,
        "change": change,
    }


# ---------------- 格式化输出(供飞书 Bot 用) ----------------


def fmt_lhb(rows):
    """龙虎榜格式化为 markdown 文本。"""
    if not rows:
        return "近期无龙虎榜数据"
    lines = [f"📰 **龙虎榜**({rows[0].get('date', '')} 共 {len(rows)} 条)", ""]
    for i, r in enumerate(rows[:10], 1):
        net = r.get("net_amt") or 0
        net_yi = round(float(net) / 1e8, 2) if net else 0
        net_str = f"净买入 {net_yi:+.2f}亿" if net_yi >= 0 else f"净卖出 {abs(net_yi):.2f}亿"
        pct = r.get("pct") or 0
        lines.append(
            f"{i}. **{r['name']}**({r['code']}) 收{r.get('close', '-')} 涨跌{pct:+.2f}% {net_str}"
        )
        if r.get("reason"):
            lines.append(f"   上榜原因: {r['reason']}")
        if r.get("explain"):
            lines.append(f"   备注: {r['explain']}")
    return "\n".join(lines)


def fmt_north_flow(rows):
    """北向资金格式化。"""
    if not rows:
        return "无北向资金数据"
    lines = [f"📈 **北向资金近 {len(rows)} 日**", ""]
    for r in rows:
        total = r.get("total_net")
        if total is None:
            continue
        icon = "🟢" if total >= 0 else "🔴"
        lines.append(
            f"{icon} {r['date']}: 沪股通 {r.get('hk2sh_net', 0):+.2f}亿 + "
            f"深股通 {r.get('hk2sz_net', 0):+.2f}亿 = 北向合计 {total:+.2f}亿"
        )
    return "\n".join(lines)


def fmt_main_flow(d):
    """主力资金流格式化。"""
    if "error" in d:
        return f"❌ {d['error']}"
    lines = [
        f"💰 **{d.get('name', '-')}**({d.get('code', '-')}) 主力资金流",
        "",
        f"- 主力净流入: {d.get('main_net', 0):+.2f}亿 (占比 {d.get('main_pct', 0):.2f}%)",
        f"- 超大单: {d.get('super_large', 0):+.2f}亿",
        f"- 大单: {d.get('large', 0):+.2f}亿",
        f"- 中单: {d.get('medium', 0):+.2f}亿",
        f"- 小单: {d.get('small', 0):+.2f}亿",
    ]
    return "\n".join(lines)


def fmt_concept_sectors(sectors):
    """概念板块反查格式化。"""
    if not sectors:
        return "无板块数据(可能代码错或接口异常)"
    lines = [f"🏷️ **属于以下板块**({len(sectors)} 个)", ""]
    for s in sectors:
        lines.append(f"- {s['board_name']}({s['board_code']}) - {s['board_type']}")
    return "\n".join(lines)


def fmt_index(data):
    """指数行情格式化。"""
    if isinstance(data, dict) and "error" in data:
        return f"❌ {data['error']}"
    if isinstance(data, dict):
        data = [data]
    lines = ["📊 **指数行情**", ""]
    for d in data:
        icon = "🟢" if (d.get("pct") or 0) >= 0 else "🔴"
        lines.append(
            f"{icon} **{d['name']}**({d['code']}) {d['price']} "
            f"{'+' if (d.get('pct') or 0) >= 0 else ''}{d.get('pct', 0)}% ({d.get('change', 0):+.2f})"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "lhb":
        print(fmt_lhb(fetch_lhb()))
    elif cmd == "north":
        print(fmt_north_flow(fetch_north_flow(days=5)))
    elif cmd == "flow":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        print(fmt_main_flow(fetch_main_flow(code)))
    elif cmd == "concept":
        code = sys.argv[2] if len(sys.argv) > 2 else "600519"
        print(fmt_concept_sectors(fetch_concept_sectors(code)))
    elif cmd == "index":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        print(fmt_index(fetch_index(name)))
    else:
        print("用法: python stock_market_extras.py [lhb|north|flow|concept|index] [args]")
