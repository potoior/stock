"""一句话策略选股 · 沙箱 Agent

自然语言策略 → LLM 写 strategy(df) -> bool → 沙箱全市场扫描。

流程(借鉴 Code Interpreter / opencode 自愈模式):
  1. 缓存命中: 同一句话直接复用已生成的代码
  2. LLM 代码生成(数据契约 + few-shot)
  3. 样本验证: 3 只真实股票试跑,报错回灌 LLM 重写(最多 3 轮)
  4. 全市场扫描: fork 子进程执行,总超时,主进程零风险
"""

import hashlib
import json
import multiprocessing
from pathlib import Path

from ai_decider import AIDecider
from strategy_engine import CACHE_DB, _bulk_fetch_daily

ENGINE_HOME = Path(__file__).parent
CACHE_PATH = ENGINE_HOME / "scan_custom_cache.json"

CODE_GEN_PROMPT = """你是 A 股量化策略工程师。请把用户的一句话选股需求翻译为一个 Python 策略函数。

## 数据契约
- df 是 pandas.DataFrame,按日期升序,约 320 行,列:
  - open, close, high, low, volume (float)
  - date (YYYYMMDD 字符串)
- 最后一行是最新交易日
- 只能 import pandas / numpy / math
- 函数签名: strategy(df) -> bool,True = 该股命中(值得买入)

## 示例
需求「MACD金叉且量比大于1.5」:
```python
import pandas as pd

def strategy(df):
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    today_golden = dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]
    vol_ratio = df["volume"].iloc[-1] / df["volume"].iloc[-6:-1].mean()
    return bool(today_golden and vol_ratio > 1.5)
```

需求「收盘价创60日新高且当天放量」:
```python
import pandas as pd

def strategy(df):
    if len(df) < 60:
        return False
    close = df["close"]
    new_high = close.iloc[-1] >= close.iloc[-61:].max()
    vol_ratio = df["volume"].iloc[-1] / df["volume"].iloc[-6:-1].mean()
    return bool(new_high and vol_ratio > 1.5)
```

## 要求
- 只输出一个 Python 代码块(```python ... ```),不要其他解释
- 数据不足时返回 False,不抛异常
- 不要用未来数据,只用已收盘的 K 线
- 精确实现需求语义,不加需求之外的条件

## 需求
「{description}」"""

SAMPLE_CODES = ["600519", "000001", "300750"]
MAX_HEAL_ROUNDS = 3
SCAN_TIMEOUT_SEC = 600  # 全市场扫描总超时


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        pass


def _desc_key(description: str) -> str:
    return hashlib.sha256(" ".join(description.split()).encode("utf-8")).hexdigest()[:16]


def extract_code(raw: str) -> str | None:
    """从 LLM 输出中提取 python 代码块。"""
    import re

    m = re.search(r"```(?:python)?\s*(.*?)```", raw, re.S)
    if not m:
        # 无代码块的裸代码:整个段当作代码(只要像函数定义)
        body = raw.strip()
        return body if "def strategy" in body else None
    return m.group(1).strip()


def _build_namespace():
    """沙箱命名空间:只暴露 pandas/numpy/math。"""
    import math

    import numpy as np
    import pandas as pd

    return {"pd": pd, "np": np, "math": math, "__builtins__": __builtins__}


def _is_bool_like(out) -> bool:
    """Python bool / numpy.bool_ 都算合法返回值。"""
    import numpy as np

    return isinstance(out, bool) or isinstance(out, np.bool_)


def run_strategy(code: str, df) -> bool:
    """在受限命名空间中执行策略函数,返回 bool。任何异常返回 False。"""
    try:
        ns = _build_namespace()
        exec(code, ns)  # noqa: S102 - 个人量化系统,LLM 代码在受限命名空间执行
        fn = ns.get("strategy")
        if fn is None:
            return False
        out = fn(df)
        if _is_bool_like(out):
            return bool(out)
        if isinstance(out, (int, float)) and out in (0, 1):
            return bool(out)
        return False
    except Exception:
        return False


def _validate_code(code: str, samples: dict) -> tuple[bool, str]:
    """样本验证:3 只真实股票试跑。返回 (通过, 错误信息)。"""
    try:
        ns = _build_namespace()
        exec(code, ns)  # noqa: S102
        if not callable(ns.get("strategy")):
            return False, "代码中未定义 strategy(df) 函数"
        for code_, df in samples.items():
            try:
                out = ns["strategy"](df)
                if not _is_bool_like(out):
                    return False, f"strategy({code_}) 返回了非布尔值: {type(out).__name__}"
            except Exception as e:
                return False, f"strategy({code_}) 抛出异常: {type(e).__name__}: {e}"
        return True, ""
    except SyntaxError as e:
        return False, f"语法错误: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _sandbox_scan(args):
    """子进程入口:扫描全市场,返回命中列表。(fork 继承 grouped,零拷贝)"""
    import signal

    code, grouped, min_amount_yi = args

    # 编译一次,循环内只调用(fork 后闭包变量零拷贝)
    ns = _build_namespace()
    exec(code, ns)  # noqa: S102 - 沙箱受限命名空间
    fn = ns.get("strategy")
    if fn is None or not callable(fn):
        return []

    def _timeout(_sig, _frm):
        raise TimeoutError("scan timeout")

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(SCAN_TIMEOUT_SEC)

    hits = []
    try:
        for code_id, df in grouped.items():
            try:
                if len(df) < 2:
                    continue
                out = fn(df)
                if not _is_bool_like(out):
                    continue
                if not bool(out):
                    continue
                last = df.iloc[-1]
                price = float(last["close"])
                prev = float(df["close"].iloc[-2])
                pct = (price - prev) / prev * 100 if prev > 0 else 0
                amount_yi = float(last["volume"] * price) / 1e8
                if amount_yi < min_amount_yi:
                    continue
                hits.append({
                    "code": code_id,
                    "price": round(price, 2),
                    "pct": round(pct, 2),
                    "amount_yi": round(amount_yi, 2),
                })
            except Exception:
                continue
        signal.alarm(0)
    except TimeoutError:
        pass
    return hits


def scan_custom(
    description: str,
    top_n: int = 20,
    min_amount_yi: float = 0.5,
    limit: int = 0,
    progress_callback=None,
) -> dict:
    """一句话策略全市场选股。

    Returns: {"ok": True, "code": str, "hits": [...], "cached": bool, ...} 或 {"error": ...}
    """
    # 1. 缓存
    cache = _load_cache()
    key = _desc_key(description)
    cached = True
    code = cache.get(key)
    if not code:
        cached = False
        # 2. LLM 生成 + 3. 样本验证 + 自愈循环
        decider = AIDecider()
        samples = _load_samples()
        if not samples:
            return {"error": "样本数据不可用,请先运行一次 daily_scan 抓取数据"}
        err = "首次编译"
        for _round in range(MAX_HEAL_ROUNDS):
            raw = decider.generate(
                CODE_GEN_PROMPT.format(description=description), timeout=120
            )
            if raw.startswith(("API错误", "调用失败", "API限流")):
                return {"error": f"AI 代码生成失败: {raw}"}
            code = extract_code(raw or "")
            if not code:
                return {"error": "AI 未返回有效的策略代码"}
            ok, err = _validate_code(code, samples)
            if ok:
                break
            err = f"第 {_round + 1} 轮生成的代码验证失败: {err}"
            code = None
        if not code:
            return {"error": f"策略代码验证未通过: {err}"}
        cache[key] = code
        _save_cache(cache)

    # 4. 全市场扫描(fork 子进程,总超时保护)
    candidates = _load_candidates(limit)
    if not candidates:
        return {"error": "daily 表为空,请先运行 daily_scan 抓取数据"}
    if progress_callback:
        progress_callback(0, len(candidates), 0)
    grouped = _bulk_fetch_daily(candidates, days=320)
    if not grouped:
        return {"error": "未加载到日 K 线数据"}

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    p = ctx.Process(target=lambda: q.put(_sandbox_scan((code, grouped, min_amount_yi))))
    p.start()
    try:
        p.join(SCAN_TIMEOUT_SEC + 30)
        if p.is_alive():
            p.terminate()
            p.join(5)
            return {"error": f"扫描超时(>{SCAN_TIMEOUT_SEC}s),已终止"}
        hits = q.get_nowait() if not q.empty() else []
    finally:
        if p.is_alive():
            p.terminate()
            p.join(5)

    hits.sort(key=lambda x: x["pct"], reverse=True)
    if progress_callback:
        progress_callback(len(candidates), len(candidates), len(hits))
    return {
        "ok": True,
        "code": code,
        "cached": cached,
        "scanned": len(candidates),
        "elapsed_sec": 0,
        "hits": hits[:top_n],
        "total_hits": len(hits),
    }


def _load_samples() -> dict:
    """从 daily 表加载 3 只样本股票的 df,用于策略验证。"""
    import sqlite3

    import pandas as pd


    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        grouped = {}
        for c in SAMPLE_CODES:
            cur = conn.execute(
                "SELECT date, open, close, high, low, volume FROM daily "
                "WHERE code=? ORDER BY date DESC LIMIT 320",
                (c,),
            )
            rows = cur.fetchall()
            if rows:
                rows.reverse()
                grouped[c] = pd.DataFrame(
                    rows, columns=["date", "open", "close", "high", "low", "volume"]
                )
        return grouped
    except Exception:
        return {}
    finally:
        conn.close()


def _load_candidates(limit: int = 0) -> list[str]:
    """最新 7 天有数据的股票候选池(跳过 ST/退市)。"""
    import sqlite3
    from datetime import datetime, timedelta


    conn = sqlite3.connect(str(CACHE_DB), timeout=30)
    try:
        latest = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        if not latest:
            return []
        if isinstance(latest, str):
            cutoff = (datetime.strptime(latest, "%Y%m%d") - timedelta(days=7)).strftime(
                "%Y%m%d"
            )
        else:
            cutoff = latest
        rows = conn.execute(
            "SELECT DISTINCT code FROM daily WHERE date >= ?", (cutoff,)
        ).fetchall()
    finally:
        conn.close()
    candidates = [r[0] for r in rows]

    try:
        import stock_names as sn

        name_map = sn.lookup_names(candidates)
        candidates = [
            c for c in candidates
            if "ST" not in (name_map.get(c, "") or "") and "退" not in (name_map.get(c, "") or "")
        ]
    except Exception:
        pass
    if limit and limit < len(candidates):
        candidates = candidates[:limit]
    return candidates
