#!/usr/bin/env python3
"""从《中国股市操练大全》提取交易策略章节正文，去图片残标，生成速查目录+正文文件。"""
import re
from pathlib import Path

SRC = Path(__file__).parent / "中国股市操练大全.md"
OUT = Path(__file__).parent / "中国股市操练大全_交易策略.md"

lines = SRC.read_text(encoding="utf-8").split("\n")


def find_header(pattern):
    """返回第一个匹配标题行号（0-indexed），无则None"""
    for i, l in enumerate(lines):
        if re.match(pattern, l):
            return i
    return None


# 各目标章节起止（按标题边界）
ranges = [
    # (主题, 起标题正则, 止标题正则)
    ("第8章 技术指标法则", r"^## 8\.2 技术指标的常用法则",
     r"^## 9\.1 基本面分析概述"),
    ("第12章 投资法则与策略", r"^## 12\.1 计划你的交易",
     r"^## 13\.1 股民遭受惨重损失"),
    ("第13章 避免惨重损失", r"^## 13\.1 股民遭受惨重损失",
     r"^# 操盘策略篇"),
    ("第14章 选股策略", r"^## 14\.1 选股的重要性",
     r"^## 15\.1 底部概述"),
    ("第15章 抄底策略", r"^## 15\.1 底部概述",
     r"^## 16\.1 顶部概述"),
    ("第16章 逃顶策略", r"^## 16\.1 顶部概述",
     r"^## 17\.1 庄家概述"),
    ("第17章 跟庄炒股", r"^## 17\.1 庄家概述",
     r"练习.*|^## 18\.1 看盘概述|^# 第18章"),
    ("第20章 涨停板策略", r"^## 20\.1 涨停板概述",
     r"^# 扩展知识篇"),
]


# 图片残标过滤规则
PCT = re.compile(r"^[-+]?\d{1,4}(\.\d+)?\s*[-+]?\d+\.\d+%?\s*$")          # 20.71 1.74%
HPCT = re.compile(r"^#+\s*[-+]?\d{1,4}(\.\d+)?\s*[-+]?\d+\.\d+%\s*$")     # ## 20.19 10.03%
PCT_ONLY = re.compile(r"^[-+]?\d{1,4}(\.\d+)?%$")                          # 8.40% 刻度行
NUM = re.compile(r"^[-+]?\d{1,6}(\.\d+)?$")                                 # 6124.04 / 264552
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")                                   # 2008-02-04
DATEFRAG = re.compile(r"^\d{4}-?\d?$|^[0-9]{2} [0-9]{2} [0-9]{2}$|^[0-9]{2} [0-9]{2}$|^200[0-9]-0$|^\d{4}-\d{2}-\d{2}|^[0-9]{2} [0-9]{2} [0-9]{2} [0-9] \d{4}-\d{2}-\d{2} \d{2}$")  # 2009-0 / 04 05 06 / 2007-08-29 三 / 04 05 06 0 2009-07-16 06
IPC = re.compile(r"^[\u4e00-\u9fa5A-Za-z·]{2,8}\s+\d{6}$")                   # 宏达新材 002211
IND = re.compile(
    r"MAVOL\d|MA\d+\s*:\s*[-+]?\d|"
    r"(^|[\s:])(总手|成交量额|成交额|合手|换手)\s*[:：]?\s*[-+]?(手|[\d,\.]+)|"   # 形态匹配避免误删正文"换手整理"等
    r"MACD\(\d|KDJ\(\d|BIAS\(\d|RSI\(\d|DIFF:\s*[-+]?\d"                       # 指标数值行
)
# 行情快照块（表头/数据行）
QHEAD = re.compile(r"^(日线|周线|月线|\w*线)\s*\(?[复权]?\)?\s*[\u4e00-\u9fa5A-Z]+")
QQT = re.compile(r"^(最新|昨收|开盘|最高|最低|涨跌|涨幅|振幅|收盘|均价线|均价|现手|量比|委比|委买量|委卖量|总市值|流通市值|市盈\(|市净率|换手|总手|成交额|上涨家数|下跌家数|平盘家数|买金额|卖金额|总手  )\s*[:：]?")
PAGE = re.compile(r"^[-—]+\s*\d+\s*$|^\d+\s+[-—]|——\s*[-+]?\d{1,4}(\.\d+)?\s*$")  # 向下洗盘 — 11.65
PERCENT = re.compile(r"^(GAOX|PERCENT|DIRECTION|CLOSE|UP|DOWN)\w+$")
DUPRE = re.compile(r"^再仔细看|^或者是|^让我们看|^不对，那是|^好吧|^等等|^实际上是|^放大看|^让我们再|^不对，通常|^实际上，那个词可能是|^那个词可能")
SELF = re.compile(r"^(用户希望我识别|自我修正|\(自我修正|让我先|让我看那个|让我们重新看|让我们看字形|让我们尝试|让我再仔细|答案是|图片是|正文内容|包含对|下一个大章节|子章节标题|底部有页码|顶部有页眉|主要章节标题|含有几个小点|段落[0-9０-９]|列表项|小标题|大标题)")
SELF3 = re.compile(r"^\d\*\*")   # 1**分析图片内容**：等工具步骤
SELF2 = re.compile(r"['“”]市盈率，全称|这个说法在原文中|我必须照抄|注意.*这个写法|看那个字形")
STARLABEL = re.compile(r"^\*[^*]")   # 单星号开头（OCR 工具噪声）；**加粗**双星号保留
# 独立图表标注词（仅作为整行出现时删除）
LABEL = re.compile(r"^(G|X10|X100|K10|K100|0轴|正值区域|负值区域|巨阳线|指标说明|介入点|成交量|指标说明|水印|[\u4e00-\u9fa5]{2,4}(顶背离|底背离))$")
WATERMARK = re.compile(r"同花顺|统计: 本指标|由.*提供")


def is_noise(line):
    s = line.strip()
    if not s:
        return False  # 空行保留
    # 表头/行情快照行：只在非标题（非##开头）且无句读的长句判断，避免误删正文
    if PCT.match(s) or HPCT.match(s) or PCT_ONLY.match(s) or NUM.match(s):
        return True
    if DATE.match(s):
        return True
    if DATEFRAG.match(s):
        return True
    if IPC.match(s):
        return True
    if IND.search(s):
        return True
    if QQT.match(s):
        return True
    if PAGE.search(s):
        return True
    if PERCENT.match(s):
        return True
    if WATERMARK.search(s):
        return True
    if LABEL.match(s):
        return True
    if DUPRE.match(s) or SELF.match(s) or SELF2.search(s) or SELF3.match(s) or STARLABEL.match(s):
        return True
    # 日线/周线/月线 + 股票名的表头残标，且该行不包含较长中文描述（长度<18）才删
    if QHEAD.match(s) and len(s) < 18:
        return True
    # 跨篇过渡标题（如 # 看盘实战篇）不属于正文章节
    if re.match(r"^#\s*[一二三四五六]篇.*|^#\s*识底|^#\s*识别|^#\s*防|^#\s*选股|^#\s*跟庄|^#\s*看盘|^#\s*盘口|^#\s*涨停", s):
        return True
    return False


def extract(start_pat, end_pat):
    start = find_header(start_pat)
    end = find_header(end_pat) if end_pat else len(lines)
    if start is None:
        return []
    if end is None or end <= start:
        end = len(lines)
    return lines[start:end]


def clean(block):
    out = []
    for l in block:
        if is_noise(l):
            continue
        out.append(l.rstrip())
    # 合并多余的连续空行
    res = []
    prev_blank = False
    for l in out:
        blank = (l.strip() == "")
        if blank and prev_blank:
            continue
        res.append(l)
        prev_blank = blank
    return res


def main():
    parts = []
    toc = []
    for theme, sp, ep in ranges:
        block = extract(sp, ep)
        block = clean(block)
        parts.append((theme, block))
        # 提取小节标题做目录
        headers = [l for l in block if l.startswith("### ") or (l.startswith("## ") and not l.startswith(sp))]
        # 恢复源文件OCR缺失的14.4/14.4.1 标题（正文在，标题被源OCR吞掉）
        if any("市盈率是考察股票投资价值的静态参考指标" in l for l in block):
            ins = [h for h in block if h.startswith("市盈率是考察股票投资价值的静态参考指标")]
            if ins:
                idx = next((i for i, h in enumerate(headers) if h.startswith("### 14.4.2")), len(headers))
                headers.insert(idx, "## 14.4 利用市盈率来选股")
                headers.insert(idx + 1, "### 14.4.1 什么是市盈率")
        toc.append((theme, headers))

    # 生成正文文件（速查目录在前，正文在后）
    out = []
    out.append("# 《中国股市操练大全》交易策略摘录\n")
    out.append("> 来源：`book/中国股市操练大全.md` ｜ 王真 张振东 柳琪 编著 中国青年出版社 ｜ ")
    out.append("> 本文档（生成版）保留策略/法则/技巧正文与提醒框，去除 OCR 图片残标。\n")

    # 速查目录
    out.append("## 速查目录\n")
    for theme, headers in toc:
        out.append(f"- **{theme}**")
        for h in headers:
            hh = h.lstrip("#").strip()
            out.append(f"    - {hh}")
        out.append("")
    out.append("---\n")

    # 正文
    for theme, block in parts:
        out.append(f"## {theme}\n")
        for l in block:
            # 恢复源文件OCR缺失的14.4/14.4.1 标题（正文在，标题被源OCR吞掉）
            if l.startswith("市盈率是考察股票投资价值的静态参考指标"):
                out.append("## 14.4 利用市盈率来选股")
                out.append("")
                out.append("### 14.4.1 什么是市盈率")
                out.append("")
            out.append(l)
        out.append("")
    OUT.write_text("\n".join(out), encoding="utf-8")

    # 打印统计
    total = sum(len(b) for _, b in parts)
    print(f"写入: {OUT}")
    print(f"总正文行数: {total}")
    for theme, b in parts:
        print(f"  - {theme}: {len(b)} 行")


if __name__ == "__main__":
    main()
