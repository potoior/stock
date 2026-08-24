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


# ---------------- 板块资金流 ----------------

# 板块类型 fs 参数:
#   m:90+t:2  行业板块
#   m:90+t:3  概念板块
SECTOR_FS = {
    "industry": "m:90+t:2",
    "concept": "m:90+t:3",
}


def fetch_sector_flow(sector_type: str = "industry", top_n: int = 10) -> list[dict]:
    """获取行业/概念板块主力资金流排名(按主力净流入降序)。

    Args:
        sector_type: "industry"=行业板块, "concept"=概念板块
        top_n: 返回前 N 个板块

    Returns: [{code, name, pct, main_net, main_pct, super_large_net, large_net}, ...]
             金额单位均为元
    """
    fs = SECTOR_FS.get(sector_type)
    if not fs:
        return [{"error": f"未知板块类型: {sector_type},支持: industry / concept"}]
    url = (
        f"http://push2.eastmoney.com/api/qt/clist/get?pn=1&pz={top_n}&po=1&np=1"
        f"&fltt=2&invt=2&fid=f62&fs={fs}"
        f"&fields=f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78"
    )
    data = _get_json(url, referer="https://data.eastmoney.com/")
    if not data or not data.get("data") or not data["data"].get("diff"):
        return []
    out = []
    for r in data["data"]["diff"]:
        out.append({
            "code": r.get("f12", ""),
            "name": r.get("f14", ""),
            "pct": r.get("f3"),       # 板块涨跌幅 %
            "main_net": r.get("f62"),    # 主力净流入(元)
            "main_pct": r.get("f184"),   # 主力净流入占比 %
            "super_large_net": r.get("f66"),  # 超大单净流入
            "large_net": r.get("f72"),       # 大单净流入
            "medium_net": r.get("f75"),      # 中单净流入
            "small_net": r.get("f78"),       # 小单净流入
        })
    return out


def fetch_market_sentiment() -> dict:
    """市场情绪指标:5大指数涨跌 + 行业板块资金流前 5 + 概念板块前 5。

    Returns: {
        indices: [{name, pct, change}],  # 5 大指数
        top_industries: [...],  # 行业板块资金流前 5
        top_concepts: [...],    # 概念板块资金流前 5
    }
    """
    indices = fetch_index() or []
    idx_brief = [
        {"name": d.get("name"), "pct": d.get("pct"), "change": d.get("change"),
         "amount_yi": round(d["amount"] / 1e8, 0) if d.get("amount") else None}
        for d in indices if isinstance(d, dict) and "error" not in d
    ]
    return {
        "indices": idx_brief,
        "top_industries": fetch_sector_flow("industry", top_n=5),
        "top_concepts": fetch_sector_flow("concept", top_n=5),
    }


# ---------------- 条件选股(PE/PB/市值) ----------------

# 全 A 股 fs 参数(沪深主板 + 创业板 + 科创板 + 北交所)
MARKET_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def screen_stocks(
    pe_max: float | None = None,
    pe_min: float | None = None,
    pb_max: float | None = None,
    pb_min: float | None = None,
    mv_min_yi: float | None = None,
    mv_max_yi: float | None = None,
    top_n: int = 30,
    sort_by: str = "pe",
) -> list[dict]:
    """全市场条件选股(PE/PB/市值筛选),用东财 clist 接口一次拉取。

    Args:
        pe_max/pe_min: PE_TTM 范围(负值=亏损,会被过滤)
        pb_max/pb_min: PB 范围
        mv_min_yi/mv_max_yi: 总市值范围(亿元)
        top_n: 返回前 N 只
        sort_by: 排序字段,pe/pb/mv

    Returns: list[dict] 或 {"error": str}(首页全失败时)
             list 元素: {code, name, price, pct, pe_ttm, pb, total_mv_yi}

    耗时: 5-15 秒(10 页 × 0.5s)
    """
    import time
    out: list[dict] = []
    pages_failed = 0  # 连续失败页数
    pages_ok = 0
    # 分页拉取全市场(含北交所/B股,客户端过滤)
    # 注意: pz=100 易被东财限频 502,用 pz=20 + sleep(0.5) 更稳定
    # 最多拉 200 只(10 页 × 20),够筛选 Top30 用
    for pn in range(1, 11):
        url = (
            f"http://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=20&po=1&np=1"
            f"&fltt=2&invt=2&fid=f3&fs={MARKET_FS}"
            f"&fields=f12,f14,f2,f3,f162,f167,f116"
        )
        # 重试 2 次(东财偶发 502)
        data = None
        for _ in range(2):
            data = _get_json(url, referer="https://data.eastmoney.com/")
            if data:
                break
            time.sleep(1.0)
        if not data or not data.get("data") or not data["data"].get("diff"):
            pages_failed += 1
            # 首页就失败:接口限频/不可用,返 error 让 handler 给友好提示
            if pn == 1:
                return {"error": "东财接口暂时不可用(可能限频),请稍后重试"}
            # 后续页失败:可能到尾页了,用已拉到的数据
            if pages_failed >= 3:
                break
            continue
        pages_ok += 1
        pages_failed = 0  # 重置连续失败计数
        for r in data["data"]["diff"]:
            code = r.get("f12", "")
            # 过滤北交所(8 开头)和 B 股(2 开头 9 结尾),只要 A 股
            if not code or code[0] not in "036":
                continue
            pe = r.get("f162")
            pb = r.get("f167")
            mv = r.get("f116")  # 总市值(元)
            # PE/PB 为 '-' 或 None 时跳过(亏损/未披露)
            if not isinstance(pe, (int, float)) or not isinstance(pb, (int, float)):
                continue
            if not isinstance(mv, (int, float)) or mv <= 0:
                continue
            mv_yi = mv / 1e8
            # 应用过滤
            # 亏损股(PE<=0): 仅当 pe_max>0(用户找正 PE)时过滤;
            # pe_max<=0 或 pe_min<0 表示用户主动找亏损股,放行
            if pe <= 0 and pe_max is not None and pe_max > 0:
                continue
            if pe_max is not None and pe > pe_max:
                continue
            if pe_min is not None and pe < pe_min:
                continue
            if pb_max is not None and pb > pb_max:
                continue
            if pb_min is not None and pb < pb_min:
                continue
            if mv_min_yi is not None and mv_yi < mv_min_yi:
                continue
            if mv_max_yi is not None and mv_yi > mv_max_yi:
                continue
            out.append({
                "code": code,
                "name": r.get("f14", ""),
                "price": r.get("f2"),
                "pct": r.get("f3"),
                "pe_ttm": round(pe, 2),
                "pb": round(pb, 2),
                "total_mv_yi": round(mv_yi, 1),
            })
            if len(out) >= top_n * 3:  # 多拉一些再排
                break
        if len(out) >= top_n * 3:
            break
        time.sleep(0.3)  # 防封
    # 按 sort_by 排序: pe 升序(低估值优先),pb/mv 降序
    if sort_by == "pe":
        out.sort(key=lambda x: x["pe_ttm"])
    elif sort_by == "pb":
        out.sort(key=lambda x: -x["pb"])
    else:
        out.sort(key=lambda x: -x["total_mv_yi"])
    return out[:top_n]


def fmt_screen_result(rows, conditions: str = "") -> str:
    """条件选股结果格式化。"""
    if not rows:
        return f"📭 无符合条件的股票\n筛选条件: {conditions or '无'}"
    lines = [f"🔍 **条件选股** {conditions}".rstrip()]
    lines.append(f"共找到 {len(rows)} 只(显示前 {len(rows)})")
    lines.append("")
    lines.append("| 代码 | 名称 | 现价 | 涨幅 | PE | PB | 市值(亿) |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        pct = r.get("pct") or 0
        icon = "🔴" if pct >= 0 else "🟢"
        lines.append(
            f"| {r['code']} | {r['name']} | {r['price']} | {icon}{pct:+.2f}% | "
            f"{r['pe_ttm']} | {r['pb']} | {r['total_mv_yi']:.0f} |"
        )
    return "\n".join(lines)


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
    # f43 最新价 / f44 最高 / f45 最低 / f46 今开 / f60 昨收
    # f48 成交额(元) / f50 量比(×100) / f168 换手率(×100)
    # f169 涨跌额 / f170 涨跌幅 / f171 振幅(×100)
    url = (
        f"http://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
        f"&fields=f57,f58,f43,f44,f45,f46,f60,f48,f50,f168,f169,f170,f171"
    )
    data = _get_json(url)
    if not data or not data.get("data"):
        return {"error": f"获取 {name} 失败"}
    d = data["data"]
    # 东财 push2 指数接口价格类字段都是 ×100 整数
    def _r(v, div=100):
        return round(float(v) / div, 2) if isinstance(v, (int, float)) else None
    price = _r(d.get("f43"))
    return {
        "name": name,
        "code": d.get("f57", ""),
        "price": price,
        "pct": _r(d.get("f170")),
        "change": _r(d.get("f169")),
        "open": _r(d.get("f46")),
        "high": _r(d.get("f44")),
        "low": _r(d.get("f45")),
        "pre_close": _r(d.get("f60")),
        "amount": d.get("f48"),  # 成交额(元)
        "amplitude": _r(d.get("f171")),  # 振幅 %
        "qr": _r(d.get("f50")),  # 量比
        "turnover": _r(d.get("f168")),  # 换手率 %
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
        icon = "🔴" if total >= 0 else "🟢"  # A股惯例:红涨绿跌
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
        icon = "🔴" if (d.get("pct") or 0) >= 0 else "🟢"  # A股惯例:红涨绿跌
        pct = d.get("pct") or 0
        chg = d.get("change") or 0
        head = (
            f"{icon} **{d['name']}**({d['code']}) {d['price']} "
            f"{'+' if pct >= 0 else ''}{pct}% ({'+' if chg >= 0 else ''}{chg:.2f})"
        )
        lines.append(head)
        ohlc_parts = []
        if d.get("open") is not None:
            ohlc_parts.append(f"今开 {d['open']}")
        if d.get("high") is not None and d.get("low") is not None:
            ohlc_parts.append(f"高 {d['high']} / 低 {d['low']}")
        if d.get("pre_close") is not None:
            ohlc_parts.append(f"昨收 {d['pre_close']}")
        if d.get("amount") is not None:
            amt_yi = d["amount"] / 1e8
            ohlc_parts.append(f"成交额 {amt_yi:.0f}亿")
        if d.get("amplitude") is not None:
            ohlc_parts.append(f"振幅 {d['amplitude']}%")
        if d.get("qr") is not None:
            ohlc_parts.append(f"量比 {d['qr']}")
        if d.get("turnover") is not None:
            ohlc_parts.append(f"换手 {d['turnover']}%")
        if ohlc_parts:
            lines.append("   " + " | ".join(ohlc_parts))
    return "\n".join(lines)


def fmt_sector_flow(rows, sector_label="板块"):
    """板块资金流格式化(行业/概念通用)。"""
    if not rows:
        return f"近期无{sector_label}资金流数据"
    if isinstance(rows, list) and rows and "error" in rows[0]:
        return f"❌ {rows[0]['error']}"
    lines = [f"💰 **{sector_label}主力资金流排名**(按净流入降序)", ""]
    lines.append("| 板块 | 涨幅 | 主力净流入 | 主力占比 |")
    lines.append("|---|---|---|---|")
    for r in rows:
        pct = r.get("pct") or 0
        mn = r.get("main_net") or 0
        mp = r.get("main_pct") or 0
        # 主力净流入:亿元,保留 2 位
        mn_yi = mn / 1e8 if abs(mn) > 1e8 else mn / 1e4  # <1亿用万
        mn_str = f"{mn_yi:+.2f}亿" if abs(mn) > 1e8 else f"{mn_yi:+.0f}万"
        icon = "🔴" if pct >= 0 else "🟢"
        lines.append(
            f"| {icon}{r['name']} | {pct:+.2f}% | {mn_str} | {mp:+.2f}% |"
        )
    return "\n".join(lines)


def fmt_market_sentiment(data):
    """市场情绪格式化:指数 + 行业板块 + 概念板块。"""
    if not data:
        return "❌ 市场情绪数据为空"
    lines = ["🌡️ **市场情绪速览**", ""]
    # 1. 5 大指数
    idx = data.get("indices") or []
    if idx:
        lines.append("**主要指数**:")
        for d in idx:
            icon = "🔴" if (d.get("pct") or 0) >= 0 else "🟢"
            pct = d.get("pct") or 0
            amt = d.get("amount_yi")
            amt_str = f" 成交{amt:.0f}亿" if amt else ""
            lines.append(f"  {icon} {d['name']} {pct:+.2f}%{amt_str}")
    # 2. 行业板块资金流前 5
    top_ind = data.get("top_industries") or []
    if top_ind:
        lines.append("")
        lines.append("**行业板块资金流 Top5**:")
        for r in top_ind[:5]:
            mn = r.get("main_net") or 0
            mn_yi = mn / 1e8
            icon = "🔴" if (r.get("pct") or 0) >= 0 else "🟢"
            lines.append(f"  {icon} {r['name']} {(r.get('pct') or 0):+.2f}% 主力{mn_yi:+.2f}亿")
    # 3. 概念板块资金流前 3(原 5,改 3 防超 600 字)
    top_con = data.get("top_concepts") or []
    if top_con:
        lines.append("")
        lines.append("**概念板块资金流 Top3**:")
        for r in top_con[:3]:
            mn = r.get("main_net") or 0
            mn_yi = mn / 1e8
            icon = "🔴" if (r.get("pct") or 0) >= 0 else "🟢"
            lines.append(f"  {icon} {r['name']} {(r.get('pct') or 0):+.2f}% 主力{mn_yi:+.2f}亿")
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
    elif cmd == "sector_flow":
        st = sys.argv[2] if len(sys.argv) > 2 else "industry"
        print(fmt_sector_flow(fetch_sector_flow(st, top_n=10), "行业板块" if st == "industry" else "概念板块"))
    elif cmd == "sentiment":
        print(fmt_market_sentiment(fetch_market_sentiment()))
    elif cmd == "screen":
        # 简化:PE<20 + PB<3 + 市值>100亿
        rows = screen_stocks(pe_max=20, pb_max=3, mv_min_yi=100, top_n=20)
        print(fmt_screen_result(rows, "PE<20 + PB<3 + 市值>100亿"))
    else:
        print("用法: python stock_market_extras.py [lhb|north|flow|concept|index|sector_flow|sentiment|screen] [args]")
