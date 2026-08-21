"""股票名称 ↔ 代码 解析,支持中文/英文/拼音首字母。

数据源: 腾讯智能搜索 smartbox.gtimg.cn,按需查询 + sqlite 缓存。
匹配支持:
  - 6位代码: "600519" → 600519
  - 中文全称/简称: "贵州茅台"/"茅台" → 600519
  - 英文/拼音: "byd"/"gzmt" → 600519

用法:
  from stock_names import resolve_code, resolve_codes
  code = resolve_code("茅台")  # → "600519"
  codes = resolve_codes("分析茅台和五粮液")  # → ["600519", "000858"]
"""

import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("quant")

DB_PATH = Path(__file__).parent / "stock_cache.db"
SEARCH_URL = "http://smartbox.gtimg.cn/s3/?t=all&q={q}"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
# 6位A股代码正则(用前后非数字断言,不依赖 \b 的 ASCII 边界)
_RE_CODE = re.compile(r"(?<!\d)(60[0-3]\d{3}|00[0-2]\d{3}|30[0-4]\d{3}|688\d{3}|8\d{5}|4\d{5})(?!\d)")


def _ensure_table() -> None:
    """确保 stock_names 缓存表存在(复合主键 query+name,因为一个 query 可能对应多个 name)。"""
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    # 检查表结构,旧表(单列PK)需重建
    try:
        cols = conn.execute("PRAGMA table_info(stock_names)").fetchall()
        if cols and len(cols[0]) == 4:  # 旧表单列PK,重建
            conn.execute("DROP TABLE stock_names")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_names (
            query TEXT,
            code TEXT,
            name TEXT,
            ts INTEGER,
            PRIMARY KEY (query, name)
        )
    """)
    conn.commit()
    conn.close()


def _search_tencent(query: str) -> list[dict]:
    """调腾讯智能搜索接口,返回 [{code, name, pinyin, market}]。"""
    try:
        qe = urllib.parse.quote(query)
        url = SEARCH_URL.format(q=qe)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=5).read().decode("gbk")
        # 格式: v_hint="sh~600519~\u8d35\u5dde\u8305\u53f0~gzmt~GP-A^sz~..."
        m = re.search(r'v_hint="([^"]*)"', raw)
        if not m or m.group(1) == "N":
            return []
        items = m.group(1).split("^")
        out = []
        for item in items:
            parts = item.split("~")
            if len(parts) >= 5 and parts[4] == "GP-A":
                market = parts[0]
                code = parts[1]
                # \uXXXX 转义还原
                name = parts[2].encode().decode("unicode_escape") if "\\u" in parts[2] else parts[2]
                pinyin = parts[3]
                out.append({"code": code, "name": name, "pinyin": pinyin, "market": market})
        return out
    except Exception as e:
        log.warning("腾讯搜索 %s 失败: %s", query, e)
        return []


def _cache_get(query: str) -> list[dict] | None:
    """从缓存读,1 天内有效。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        row = conn.execute(
            "SELECT code, name FROM stock_names WHERE query=? AND ts > ?",
            (query.lower(), int(time.time()) - 86400),
        ).fetchall()
        conn.close()
        if row:
            return [{"code": r[0], "name": r[1]} for r in row]
    except Exception:
        pass
    return None


def _short_name(name: str) -> str:
    """从全称生成简称,去地名/类型词后缀。如"贵州茅台"→"茅台","中国平安"→"平安"。"""
    if not name:
        return ""
    # 常见地名前缀
    for prefix in ("中国", "中科", "中远", "中海", "中航", "中粮", "中铁", "中建",
                   "中油", "中铝", "中信", "北京", "上海", "深圳", "广州", "杭州",
                   "贵州", "四川", "山东", "江苏", "浙江", "福建", "安徽", "湖南",
                   "湖北", "河南", "河北", "山西", "陕西", "云南", "广西", "广东"):
        if name.startswith(prefix) and len(name) > len(prefix) + 1:
            return name[len(prefix):]
    return name


def _cache_put(query: str, items: list[dict]) -> None:
    """写缓存。同时缓存全称和简称,方便后续匹配。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("DELETE FROM stock_names WHERE query=?", (query.lower(),))
        rows = []
        ts = int(time.time())
        for it in items:
            name = it.get("name", "")
            rows.append((query.lower(), it["code"], name, ts))
            # 同时缓存简称(若与全称不同)
            short = _short_name(name)
            if short and short != name:
                rows.append((query.lower(), it["code"], short, ts))
        conn.executemany(
            "INSERT INTO stock_names(query, code, name, ts) VALUES(?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("写 stock_names 缓存失败: %s", e)


def resolve_code(query: str) -> str | None:
    """解析一个股票名/简称/拼音/代码,返回 6 位代码或 None。

    优先级: 6位代码 > 缓存 > 腾讯搜索
    多匹配时返回 A 股代码(排除港股/美股)。
    """
    if not query:
        return None
    q = query.strip()
    # 1. 6 位代码直接返回
    if _RE_CODE.fullmatch(q):
        return q
    _ensure_table()
    # 2. 缓存
    cached = _cache_get(q)
    if cached is not None:
        return cached[0]["code"] if cached else None
    # 3. 腾讯搜索
    items = _search_tencent(q)
    _cache_put(q, items)
    return items[0]["code"] if items else None


def _load_all_cached_names() -> dict[str, str]:
    """加载所有已缓存的股票名 → code 映射,用于多股票文本快速扫描。"""
    out = {}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute("SELECT name, code FROM stock_names WHERE name != ''").fetchall()
        conn.close()
        for name, code in rows:
            if name and code:
                out[name] = code
    except Exception:
        pass
    return out


def lookup_names(codes: list[str]) -> dict[str, str]:
    """批量查 code → name 映射(只读 sqlite 缓存,不联网)。

    供扫描结果展示用:已知一堆代码,需要批量补名称。
    缓存未命中的 code 不出现在返回 dict 中(调用方自行用空串兜底)。
    """
    if not codes:
        return {}
    _ensure_table()
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        # 用 IN(...) 一次性查全部,避免 N+1
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT code, name FROM stock_names WHERE code IN ({placeholders}) AND name != ''",
            codes,
        ).fetchall()
        conn.close()
        for code, name in rows:
            if name:
                out[code] = name
    except Exception:
        pass
    return out


def resolve_codes(text: str) -> list[str]:
    """从一段文本中提取所有股票代码或名称,返回去重后的代码列表。

    策略: 先抠 6 位代码,再用已缓存股票名做最长匹配扫描。
    缓存未命中时,只对单只股票的查询触发实时搜索(resolve_code)。
    """
    if not text:
        return []
    found: list[str] = []
    # 1. 6 位代码
    for m in _RE_CODE.findall(text):
        if m not in found:
            found.append(m)
    # 2. 用已缓存股票名扫描(长名优先避免短名误匹配)
    names_map = _load_all_cached_names()
    if names_map:
        sorted_names = sorted(names_map.items(), key=lambda x: -len(x[0]))
        consumed = text
        for name, code in sorted_names:
            if name in consumed:
                if code not in found:
                    found.append(code)
                # 已匹配部分替换为空格,避免短名再误匹配
                consumed = consumed.replace(name, " " * len(name))
    # 3. 如果文本中只有未缓存的中文名(2-4字),尝试单独搜索
    # 简化:整个文本(去代码去停用词后)若仍含未识别的中文,调单股票 resolve
    remaining = re.sub(r"[^\u4e00-\u9fff]+", " ", text).strip()
    if remaining and not any(c in remaining for c in ["分析", "策略", "怎么样"]):
        # 取第一段中文
        first_seg = remaining.split()[0] if remaining.split() else remaining
        if 2 <= len(first_seg) <= 6:
            code = resolve_code(first_seg)
            if code and code not in found:
                found.append(code)
    return found


def refresh() -> int:
    """保留兼容接口。"""
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for q in sys.argv[1:]:
            code = resolve_code(q)
            print(f"  {q!r:15s} → {code}")
    else:
        for q in ["贵州茅台", "茅台", "五粮液", "600519", "000858", "赤天化",
                  "平安银行", "招商银行", "宁德时代", "byd", "比亚迪",
                  "正线电气", "gzmt", "mt"]:
            code = resolve_code(q)
            print(f"  {q!r:15s} → {code}")
        print()
        for text in ["分析茅台和五粮液", "看下600519和五粮液", "茅台vs五粮液谁好"]:
            codes = resolve_codes(text)
            print(f"  {text!r} → {codes}")
