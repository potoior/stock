"""飞书图片生成模块: 用 matplotlib 生成各类图表,返回 BytesIO(PNG)。

图表类型:
  gen_kline_chart(code, days=120)        K线+成交量+MACD/KDJ 副图,标买卖信号
  gen_backtest_chart(strategy_id)        回测收益曲线(策略 vs 基准 + 超额)
  gen_yujie_wall(picks)                  玉姐候选 Top 缩略图墙
  gen_market_chart(stats)                市场涨跌分布 + 板块热度

字体: Noto CJK SC(系统已装)
"""

import io
import json
import logging
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无 GUI 后端
import matplotlib.patches as mpatches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

log = logging.getLogger("quant")

ENGINE_HOME = Path(__file__).parent

# 中文字体配置(只配一次)
_FONT_INIT = False

# 图片缓存: (图表类型, code, 日期) → PNG bytes,当日有效
# gen_kline_chart 2-3 秒,同股同日重复分析时复用省 70% 时间
_IMG_CACHE: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()
_IMG_CACHE_MAX = 30  # 最多缓存 30 张图,真 LRU(命中时 move_to_end)
_IMG_CACHE_LOCK = threading.Lock()  # 飞书 Bot 多线程处理消息,需保护 OrderedDict


def _img_cache_get(chart_type: str, code: str) -> bytes | None:
    """读缓存,命中时 move_to_end 实现 LRU(最近访问的在尾部,淘汰从头部)。"""
    today = datetime.now().strftime("%Y%m%d")
    key = (chart_type, code, today)
    with _IMG_CACHE_LOCK:
        val = _IMG_CACHE.get(key)
        if val is not None:
            _IMG_CACHE.move_to_end(key)  # 命中时移到尾部,LRU
    return val


def _img_cache_put(chart_type: str, code: str, png: bytes) -> None:
    """存当日缓存,超限时从头部淘汰最久未访问的(真 LRU)。"""
    today = datetime.now().strftime("%Y%m%d")
    key = (chart_type, code, today)
    with _IMG_CACHE_LOCK:
        _IMG_CACHE[key] = png
        _IMG_CACHE.move_to_end(key)  # 新写入也放尾部
        # 超限:先清跨日旧数据,再淘汰头部最久未访问的
        if len(_IMG_CACHE) > _IMG_CACHE_MAX:
            for k in list(_IMG_CACHE.keys()):
                if k[2] != today:
                    del _IMG_CACHE[k]
        while len(_IMG_CACHE) > _IMG_CACHE_MAX:
            _IMG_CACHE.popitem(last=False)  # 头部 = 最久未访问


def _init_font():
    global _FONT_INIT
    if _FONT_INIT:
        return
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ]
    for fp in candidates:
        if Path(fp).exists():
            try:
                font_manager.fontManager.addfont(fp)
                name = font_manager.FontProperties(fname=fp).get_name()
                plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
                break
            except Exception:
                continue
    plt.rcParams["axes.unicode_minus"] = False
    _FONT_INIT = True


# 颜色
COLOR_UP = "#e53935"
COLOR_DN = "#43a047"
COLOR_MACD = "#1976d2"
COLOR_DEA = "#ff9800"
COLOR_BOLL_U = "#9e9e9e"
COLOR_BOLL_L = "#9e9e9e"
COLOR_BOLL_M = "#7e57c2"
COLOR_VOL = "#90caf9"


# ============ 个股 K 线图 ============


def _kline_data(code: str, days: int = 120):
    """加载 K 线数据 + 指标。"""
    import pandas as pd

    import strategy_engine as se

    df = se.get_daily_data(code)
    if len(df) < 30:
        return None
    # 取最近 N 天
    df = df.tail(days).reset_index(drop=True)
    # 转 date
    if "date" in df.columns:
        try:
            df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        except Exception:
            df["date"] = pd.to_datetime(df["date"])
    # 算指标
    macd_diff, macd_dea, macd_bar = se.compute_macd(df)
    k, d, j, _ = se.compute_kdj(df)
    boll_u, boll_m, boll_l = se.compute_boll(df)
    return {
        "df": df,
        "macd_diff": macd_diff, "macd_dea": macd_dea, "macd_bar": macd_bar,
        "k": k, "d": d, "j": j,
        "boll_u": boll_u, "boll_m": boll_m, "boll_l": boll_l,
    }


def gen_kline_chart(code: str, days: int = 120) -> io.BytesIO | None:
    """生成 K 线图 + 成交量 + MACD + KDJ 三联图。

    Returns:
        BytesIO(PNG) 或 None(数据不足)。同股同日复用缓存。
    """
    cached = _img_cache_get("kline", code)
    if cached:
        return io.BytesIO(cached)
    _init_font()
    try:
        data = _kline_data(code, days)
        if data is None:
            return None
        df = data["df"]
        # 跑分析取信号(标买卖点)
        import strategy_engine as se
        try:
            res = se.analyze(code, use_ai=False)
            buys = res.get("buy_reasons", [])
            sells = res.get("sell_reasons", [])
        except Exception:
            buys = sells = []

        # 名称
        rt = res.get("realtime") if res else None
        name = (rt or {}).get("name", code)
        price = (rt or {}).get("price", df["close"].iloc[-1])

        fig, axes = plt.subplots(4, 1, figsize=(10, 9), sharex=True,
                                 gridspec_kw={"height_ratios": [4, 1, 1.5, 1.5]})
        fig.subplots_adjust(hspace=0.05, left=0.08, right=0.95, top=0.93, bottom=0.07)

        x = range(len(df))
        dates = df["date"] if "date" in df.columns else x  # noqa: F841

        # --- 主图: K 线 + BOLL ---
        ax = axes[0]
        # 画 K 线柱(简化版,不用 mplfinance)
        for i in range(len(df)):
            o, c, hi, lo = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
            color = COLOR_UP if c >= o else COLOR_DN
            ax.vlines(i, lo, hi, color=color, linewidth=0.8)
            # 实体
            body_low, body_high = min(o, c), max(o, c)
            ax.add_patch(mpatches.Rectangle(
                (i - 0.3, body_low), 0.6, max(body_high - body_low, 0.001),
                facecolor=color, edgecolor=color, linewidth=0.5,
            ))
        # BOLL
        if data["boll_u"] is not None:
            ax.plot(x, data["boll_u"], color=COLOR_BOLL_U, linewidth=0.8, label="BOLL上轨")
            ax.plot(x, data["boll_l"], color=COLOR_BOLL_L, linewidth=0.8, label="BOLL下轨")
            ax.plot(x, data["boll_m"], color=COLOR_BOLL_M, linewidth=0.8, label="BOLL中轨")
            ax.fill_between(x, data["boll_u"], data["boll_l"], alpha=0.05, color="#7e57c2")
        # 标买卖信号点(取最后一个)
        if sells:
            ax.scatter([len(df) - 1], [df["close"].iloc[-1]], marker="v", s=120,
                       color=COLOR_DN, zorder=5, edgecolors="white", linewidths=1)
        if buys:
            ax.scatter([len(df) - 1], [df["close"].iloc[-1]], marker="^", s=120,
                       color=COLOR_UP, zorder=5, edgecolors="white", linewidths=1)
        ax.set_title(f"{name} ({code})  {price:.2f}  信号:{res.get('verdict','-') if res else '-'}",
                     fontsize=13, fontweight="bold")
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=8)

        # --- 成交量 ---
        ax = axes[1]
        vol = df["volume"]
        colors = [COLOR_UP if df["close"].iloc[i] >= df["open"].iloc[i] else COLOR_DN for i in range(len(df))]
        ax.bar(x, vol, color=colors, width=0.7)
        ax.set_ylabel("量", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=7)

        # --- MACD ---
        ax = axes[2]
        diff = data["macd_diff"]
        dea = data["macd_dea"]
        bar = data["macd_bar"]
        ax.plot(x, diff, color=COLOR_MACD, linewidth=1, label="DIFF")
        ax.plot(x, dea, color=COLOR_DEA, linewidth=1, label="DEA")
        bar_colors = [COLOR_UP if b >= 0 else COLOR_DN for b in bar]
        ax.bar(x, bar, color=bar_colors, width=0.7, alpha=0.6)
        ax.axhline(0, color="gray", linewidth=0.5)
        ax.set_ylabel("MACD", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=7)
        ax.tick_params(labelsize=7)

        # --- KDJ ---
        ax = axes[3]
        ax.plot(x, data["k"], color="#1976d2", linewidth=1, label="K")
        ax.plot(x, data["d"], color="#ff9800", linewidth=1, label="D")
        ax.plot(x, data["j"], color="#7e57c2", linewidth=1, label="J")
        ax.axhline(80, color="gray", linewidth=0.5, linestyle="--", alpha=0.5)
        ax.axhline(20, color="gray", linewidth=0.5, linestyle="--", alpha=0.5)
        ax.set_ylabel("KDJ", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=7)
        ax.tick_params(labelsize=7)

        # x 轴日期
        if "date" in df.columns:
            n = len(df)
            tick_step = max(1, n // 8)
            ticks = list(range(0, n, tick_step))
            ax.set_xticks(ticks)
            ax.set_xticklabels([df["date"].iloc[i].strftime("%m-%d") for i in ticks], rotation=30)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        _img_cache_put("kline", code, buf.getvalue())
        return buf
    except Exception as e:
        log.error("生成 K 线图失败 %s: %s", code, e)
        return None


# ============ 玉姐专属图表 ============


def gen_yujie_chart(code: str, score: float, hits: list, detail: dict) -> io.BytesIO | None:
    """玉姐专属图表: K线+成交量+MACD + 评分命中标注。

    与 gen_kline_chart 区别:
    - 去掉 KDJ 副图,加玉姐评分标题
    - 在 K 线主图右上角标注评分+命中规则列表
    - 在 MACD 图标注金叉/绿柱缩短等命中点

    Args:
        code: 股票代码
        score: 玉姐评分
        hits: 命中规则名列表(中文,如 ["MACD金叉","突破+金叉"])
        detail: score_stock 返回的 detail dict(含 ma5/ma10/macd_dif 等)
    """
    _init_font()
    try:
        data = _kline_data(code, days=120)
        if data is None:
            return None
        df = data["df"]

        import strategy_engine as se
        try:
            res = se.analyze(code, use_ai=False)
            rt = res.get("realtime") or {}
            name = rt.get("name", code)
            price = rt.get("price", df["close"].iloc[-1])
            pct = rt.get("pct", 0)
        except Exception:
            name = code
            price = df["close"].iloc[-1]
            pct = 0

        # 评分颜色
        if score >= 7:
            score_color = "#e53935"  # 红=强势
            score_tag = "[强势]"
        elif score >= 5:
            score_color = "#ff9800"  # 橙=中等
            score_tag = "[中等]"
        elif score >= 3:
            score_color = "#1976d2"  # 蓝=偏弱
            score_tag = "[偏弱]"
        else:
            score_color = "#9e9e9e"  # 灰=弱
            score_tag = "[弱]"

        fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True,
                                 gridspec_kw={"height_ratios": [4, 1, 1.5]})
        fig.subplots_adjust(hspace=0.05, left=0.08, right=0.95, top=0.88, bottom=0.07)

        x = range(len(df))

        # --- 主图: K 线 + BOLL ---
        ax = axes[0]
        for i in range(len(df)):
            o, c, hi, lo = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
            color = COLOR_UP if c >= o else COLOR_DN
            ax.vlines(i, lo, hi, color=color, linewidth=0.8)
            body_low, body_high = min(o, c), max(o, c)
            ax.add_patch(mpatches.Rectangle(
                (i - 0.3, body_low), 0.6, max(body_high - body_low, 0.001),
                facecolor=color, edgecolor=color, linewidth=0.5,
            ))
        # BOLL
        if data["boll_u"] is not None:
            ax.plot(x, data["boll_u"], color=COLOR_BOLL_U, linewidth=0.8, label="BOLL上轨")
            ax.plot(x, data["boll_l"], color=COLOR_BOLL_L, linewidth=0.8, label="BOLL下轨")
            ax.plot(x, data["boll_m"], color=COLOR_BOLL_M, linewidth=0.8, label="BOLL中轨")
            ax.fill_between(x, data["boll_u"], data["boll_l"], alpha=0.05, color="#7e57c2")
        # 均线
        if detail:
            for w, col, lbl in [(5, "#e53935", "MA5"), (10, "#ff9800", "MA10"),
                                (20, "#1976d2", "MA20"), (60, "#7e57c2", "MA60")]:
                key = f"ma{w}"
                if key in detail and detail[key] is not None:
                    try:
                        ma = df["close"].rolling(w).mean()
                        ax.plot(x, ma, color=col, linewidth=0.7, label=lbl, alpha=0.7)
                    except Exception:
                        pass

        # 标题含评分
        pct_emoji = "↑" if pct > 0 else "↓" if pct < 0 else "→"
        ax.set_title(
            f"{score_tag} {name} ({code})  {price:.2f} {pct_emoji} {pct:+.2f}%   "
            f"玉姐评分: {score:g} 分",
            fontsize=13, fontweight="bold", color=score_color,
        )

        # 右上角命中规则文本框
        if hits:
            hit_text = "命中规则:\n" + "\n".join(f"[+] {h}" for h in hits)
        else:
            hit_text = "未命中任何规则"
        ax.text(
            0.98, 0.02, hit_text, transform=ax.transAxes,
            fontsize=8, va="bottom", ha="right",
            bbox=dict(boxstyle="round,pad=0.4", facecolor="white", edgecolor=score_color, alpha=0.85),
        )

        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=7)

        # --- 成交量 ---
        ax = axes[1]
        vol = df["volume"]
        colors = [COLOR_UP if df["close"].iloc[i] >= df["open"].iloc[i] else COLOR_DN for i in range(len(df))]
        ax.bar(x, vol, color=colors, width=0.7)
        ax.set_ylabel("量", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.tick_params(labelsize=7)

        # --- MACD + 玉姐命中标注 ---
        ax = axes[2]
        diff = data["macd_diff"]
        dea = data["macd_dea"]
        bar = data["macd_bar"]
        ax.plot(x, diff, color=COLOR_MACD, linewidth=1, label="DIFF")
        ax.plot(x, dea, color=COLOR_DEA, linewidth=1, label="DEA")
        bar_colors = [COLOR_UP if b >= 0 else COLOR_DN for b in bar]
        ax.bar(x, bar, color=bar_colors, width=0.7, alpha=0.6)
        ax.axhline(0, color="gray", linewidth=0.5)

        # 标注玉姐命中的 MACD 相关规则
        last_idx = len(df) - 1
        annotations = []
        if "MACD金叉" in hits:
            annotations.append(("MACD金叉 +2", last_idx, float(diff.iloc[last_idx]), "#e53935"))
        if "MACD即将金叉" in hits:
            annotations.append(("即将金叉 +1", last_idx, float(diff.iloc[last_idx]), "#ff9800"))
        if "MACD绿柱缩短" in hits:
            annotations.append(("绿柱缩短 +1", last_idx, float(bar.iloc[last_idx]), "#1976d2"))
        for text, xi, yi, color in annotations:
            ax.annotate(
                text, xy=(xi, yi), xytext=(xi - 15, yi + (0.3 if yi >= 0 else -0.3)),
                fontsize=7, color=color, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color=color, lw=0.8),
            )

        ax.set_ylabel("MACD", fontsize=8)
        ax.grid(True, alpha=0.2)
        ax.legend(loc="upper left", fontsize=7)
        ax.tick_params(labelsize=7)

        # x 轴日期
        if "date" in df.columns:
            n = len(df)
            tick_step = max(1, n // 8)
            ticks = list(range(0, n, tick_step))
            ax.set_xticks(ticks)
            ax.set_xticklabels([df["date"].iloc[i].strftime("%m-%d") for i in ticks], rotation=30)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log.error("生成玉姐图失败 %s: %s", code, e)
        return None


# ============ 回测收益曲线 ============


def gen_backtest_chart(strategy_id: str) -> io.BytesIO | None:
    """回测收益曲线图: 各持有期策略收益 vs 基准 + 超额。

    数据源: builtin_backtest_report.json
    """
    _init_font()
    try:
        report_path = ENGINE_HOME / "builtin_backtest_report.json"
        if not report_path.exists():
            return None
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        strategies = rep.get("strategies", {})
        if isinstance(strategies, list):
            target = next((s for s in strategies if s.get("id") == strategy_id), None)
        else:
            target = strategies.get(strategy_id)
        if not target:
            return None

        h = target.get("horizons", {})
        baseline = rep.get("baseline", {})
        horizons = sorted([int(x) for x in h.keys()])
        rets = [h[str(horizon)].get("mean_ret", 0) * 100 for horizon in horizons]
        bases = [baseline.get(str(horizon), 0) * 100 for horizon in horizons]
        excess = [r - b for r, b in zip(rets, bases, strict=False)]

        fig, axes = plt.subplots(2, 1, figsize=(9, 6),
                                 gridspec_kw={"height_ratios": [2, 1]})
        fig.subplots_adjust(hspace=0.3, left=0.1, right=0.95, top=0.92, bottom=0.1)

        # 上图: 收益对比
        ax = axes[0]
        x = list(range(len(horizons)))
        w = 0.35
        ax.bar([i - w/2 for i in x], rets, w, label=f"策略 {strategy_id}", color="#1976d2")
        ax.bar([i + w/2 for i in x], bases, w, label="基准(全市场等权)", color="#bdbdbd")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{h}天" for h in horizons])
        ax.set_ylabel("收益 (%)")
        ax.set_title(f"策略 {target.get('name', strategy_id)} 回测收益对比", fontsize=12, fontweight="bold")
        ax.legend()
        ax.grid(True, alpha=0.2, axis="y")
        # 加数值标签
        for i, v in enumerate(rets):
            ax.text(i - w/2, v + 0.1, f"{v:+.2f}", ha="center", fontsize=8)
        for i, v in enumerate(bases):
            ax.text(i + w/2, v + 0.1, f"{v:+.2f}", ha="center", fontsize=8)

        # 下图: 超额 alpha
        ax = axes[1]
        colors = ["#e53935" if e > 0 else "#43a047" for e in excess]
        ax.bar(x, excess, color=colors, width=0.5)
        ax.axhline(0, color="gray", linewidth=0.5)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{h}天" for h in horizons])
        ax.set_ylabel("超额 alpha (%)")
        ax.set_title("超额收益(策略 - 基准)", fontsize=10)
        ax.grid(True, alpha=0.2, axis="y")
        for i, v in enumerate(excess):
            ax.text(i, v + (0.05 if v > 0 else -0.15), f"{v:+.2f}", ha="center", fontsize=9)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log.error("生成回测图失败 %s: %s", strategy_id, e)
        return None


# ============ 玉姐候选缩略图墙 ============


def gen_yujie_wall(picks: list, cols: int = 5) -> io.BytesIO | None:
    """玉姐候选 Top N 缩略图墙: 每个格子一只股票的近 60 天 K 线。"""
    _init_font()
    if not picks:
        return None
    try:
        import strategy_engine as se

        n = min(len(picks), 10)  # 最多 10 只
        picks = picks[:n]
        rows = (n + cols - 1) // cols
        fig, axes = plt.subplots(rows, cols, figsize=(cols * 2.5, rows * 2.2),
                                 squeeze=False)
        fig.subplots_adjust(hspace=0.4, wspace=0.3, left=0.03, right=0.98, top=0.93, bottom=0.05)
        fig.suptitle("玉姐精选 Top 候选 K 线缩略图", fontsize=13, fontweight="bold")

        for idx, p in enumerate(picks):
            r, c = idx // cols, idx % cols
            ax = axes[r][c]
            try:
                df = se.get_daily_data(p["code"]).tail(60).reset_index(drop=True)
                if df.empty:
                    ax.text(0.5, 0.5, "无数据", ha="center", va="center", transform=ax.transAxes)
                    continue
                # 用 _ 是占位,不实际用 x
                for i in range(len(df)):
                    o, cl, hi, lo = df["open"].iloc[i], df["close"].iloc[i], df["high"].iloc[i], df["low"].iloc[i]
                    color = COLOR_UP if cl >= o else COLOR_DN
                    ax.vlines(i, lo, hi, color=color, linewidth=0.5)
                    body_low, body_high = min(o, cl), max(o, cl)
                    ax.add_patch(mpatches.Rectangle(
                        (i - 0.3, body_low), 0.6, max(body_high - body_low, 0.001),
                        facecolor=color, edgecolor=color, linewidth=0.3,
                    ))
                ax.set_title(f"{p['rank']}.{p['code']} {p['name']}\n评分{p['score']}",
                             fontsize=8, fontweight="bold")
                ax.tick_params(labelleft=False, labelbottom=False, length=0)
                ax.grid(False)
            except Exception:
                ax.text(0.5, 0.5, "数据异常", ha="center", va="center", transform=ax.transAxes)
                ax.set_title(f"{p.get('rank')}.{p.get('code')}", fontsize=8)

        # 隐藏空格子
        for idx in range(n, rows * cols):
            r, c = idx // cols, idx % cols
            axes[r][c].axis("off")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=90, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log.error("生成玉姐墙失败: %s", e)
        return None


# ============ 市场情绪图 ============


def gen_market_chart(stats: dict) -> io.BytesIO | None:
    """市场情绪图: 涨跌分布饼图 + 涨跌停数 + 成交额。"""
    _init_font()
    try:
        fig, axes = plt.subplots(1, 2, figsize=(10, 4.5),
                                 gridspec_kw={"width_ratios": [1, 1]})
        fig.subplots_adjust(wspace=0.3, left=0.05, right=0.97, top=0.88, bottom=0.1)
        date_str = datetime.now().strftime("%Y-%m-%d")
        fig.suptitle(f"A股市场情绪 {date_str}", fontsize=13, fontweight="bold")

        # 左:涨跌分布饼图
        ax = axes[0]
        labels = ["上涨", "下跌", "平"]
        sizes = [stats.get("up", 0), stats.get("down", 0), stats.get("flat", 0)]
        colors = [COLOR_UP, COLOR_DN, "#bdbdbd"]
        # 过滤 0 值
        non_zero = [(lb, s, c) for lb, s, c in zip(labels, sizes, colors, strict=False) if s > 0]
        if non_zero:
            labels, sizes, colors = zip(*non_zero, strict=False)
            ax.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%",
                   startangle=90, textprops={"fontsize": 11})
        ax.set_title(f"涨跌分布 (总{stats.get('total', 0)}只)")

        # 右:关键指标柱图
        ax = axes[1]
        metrics = ["涨停", "跌停", "成交额(亿)"]
        # 涨跌停数 + 成交额归一化展示
        vals = [stats.get("limit_up", 0), stats.get("limit_down", 0), int(stats.get("total_amount_yi", 0))]
        # 用对数刻度因为成交额数量级大
        bars = ax.bar(metrics, vals, color=[COLOR_UP, COLOR_DN, "#1976d2"])
        ax.set_yscale("log")
        ax.set_title("关键指标(对数刻度)")
        for bar, v in zip(bars, vals, strict=False):
            ax.text(bar.get_x() + bar.get_width()/2, v * 1.1, str(v),
                    ha="center", fontsize=10, fontweight="bold")
        ax.grid(True, alpha=0.2, axis="y")

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        log.error("生成市场图失败: %s", e)
        return None


# ============ CLI ============


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--kline", metavar="CODE", help="生成 K 线图")
    ap.add_argument("--backtest", metavar="SID", help="生成回测图")
    ap.add_argument("--market", action="store_true", help="生成市场情绪图")
    ap.add_argument("--yujie", action="store_true", help="生成玉姐墙")
    ap.add_argument("--out", default="/tmp/chart.png", help="输出文件路径")
    args = ap.parse_args()

    buf = None
    if args.kline:
        buf = gen_kline_chart(args.kline)
    elif args.backtest:
        buf = gen_backtest_chart(args.backtest)
    elif args.market:
        buf = gen_market_chart({
            "total": 5338, "up": 2856, "down": 2103, "flat": 379,
            "limit_up": 42, "limit_down": 8, "total_amount_yi": 9876
        })
    elif args.yujie:
        import yujie_scan
        buf = gen_yujie_wall(yujie_scan.load_picks())

    if buf:
        Path(args.out).write_bytes(buf.getvalue())
        print(f"图片已保存: {args.out} ({len(buf.getvalue())} bytes)")
    else:
        print("生成失败")


if __name__ == "__main__":
    main()
