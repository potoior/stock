"""a-stock CLI v1.1.0 — A股量化分析命令行入口。

子命令:
  analyze   <codes...>          多股分析(56策略信号 + 综合建议)
  list                         策略列表(含开关状态/参数)
  toggle    <id> <on|off>       开关策略
  params    <id> <k=v ...>      调整策略参数
  backtest  <id> <code>         单策略个股回测
  scan      <id> [--codes ...]  策略选股(本地缓存股票池)
  library   [keyword]          查询策略大全(4来源73策略)

用法:
  uv run {baseDir}/scripts/cli.py analyze 600789
  uv run {baseDir}/scripts/cli.py toggle macd off
  uv run {baseDir}/scripts/cli.py backtest macd 600519 --days 500
  uv run {baseDir}/scripts/cli.py scan kdj --codes 600519,000001
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import strategy_engine as se
from strategy_indicators import DEFAULT_STRATEGY_PARAMS


def _params_of(strategy_id, strategies_cfg):
    """合并默认参数与用户配置参数(与 analyze 内部口径一致)。"""
    for s in strategies_cfg:
        if s.get("id") == strategy_id:
            return {**DEFAULT_STRATEGY_PARAMS.get(strategy_id, {}), **(s.get("params") or {})}
    return dict(DEFAULT_STRATEGY_PARAMS.get(strategy_id, {}))


def _enabled_map(strategies_cfg):
    return {s.get("id"): bool(s.get("enabled", True)) for s in strategies_cfg}


def cmd_analyze(codes):
    for code in codes:
        res = se.analyze(code)
        if "error" in res:
            print(f"\n{code}: {res['error']}")
            continue
        rt = res.get("realtime") or {}
        name = rt.get("name", code)
        print(f"\n{'=' * 60}")
        print(f"  A股量化分析  {rt.get('code', code)} {name}")
        if rt.get("price"):
            print(
                f"  现价: {rt['price']:.2f}   "
                f"涨跌: {rt['change']:+.2f} ({rt['pct']:+.2f}%)"
            )
        s = res["summary"]
        print(f"{'=' * 60}")
        print(
            f"  策略信号 ({s['total']}个): 买入 {s['buy']}  |  "
            f"卖出 {s['sell']}  |  观望 {s['hold']}"
        )
        if res["buy_reasons"]:
            print("\n  触发买入:")
            for x in res["buy_reasons"]:
                print(f"    [{x['name']}] {x['reason']}")
        if res["sell_reasons"]:
            print("\n  触发卖出:")
            for x in res["sell_reasons"]:
                print(f"    [{x['name']}] {x['reason']}")
        if not res["buy_reasons"] and not res["sell_reasons"]:
            print("\n  (无方向性信号)")
        print(f"\n  综合建议: {res['verdict']} {res['verdict_icon']}")
        print()


def cmd_list():
    strategies_cfg = se.get_strategies()
    enabled = _enabled_map(strategies_cfg)
    print(f"内置策略共 {len(se.BUILTIN_REGISTRY)} 个:\n")
    for sid, name, _fn in se.BUILTIN_REGISTRY:
        params = _params_of(sid, strategies_cfg)
        flag = "开" if enabled.get(sid, True) else "关"
        p_str = " ".join(f"{k}={v}" for k, v in params.items())
        print(f"  [{flag}] {sid:<24} {name}  ({p_str})")


def cmd_toggle(strategy_id, enabled):
    if strategy_id not in se.BUILTIN_STRATEGY_IDS:
        print(f"未知策略 id: {strategy_id}")
        sys.exit(1)
    strategies = se.get_strategies()
    for s in strategies:
        if s.get("id") == strategy_id:
            s["enabled"] = enabled
            break
    else:
        strategies.append(
            {"id": strategy_id, "type": "builtin", "enabled": enabled, "params": {}}
        )
    se.save_strategies(strategies)
    se.clear_ai_cache()
    state = "开启" if enabled else "关闭"
    print(f"策略 {strategy_id} 已{state} (写入 {se.CONFIG_PATH})")


def cmd_params(strategy_id, kv_pairs):
    params = {}
    for kv in kv_pairs:
        k, _, v = kv.partition("=")
        if not _:
            print(f"参数格式错误: {kv} (应为 k=v)")
            sys.exit(1)
        try:
            params[k] = int(v) if "." not in v else float(v)
        except ValueError:
            params[k] = v
    strategies = se.get_strategies()
    for s in strategies:
        if s.get("id") == strategy_id:
            cur = s.get("params", {}) or {}
            cur.update(params)
            s["params"] = cur
            break
    else:
        strategies.append(
            {"id": strategy_id, "type": "builtin", "enabled": True, "params": params}
        )
    se.save_strategies(strategies)
    se.clear_ai_cache()
    p_str = ", ".join(f"{k}={v}" for k, v in params.items())
    print(f"策略 {strategy_id} 参数已更新: {p_str}")


def cmd_backtest(strategy_id, code, days=320):
    if strategy_id not in se.BUILTIN_STRATEGY_IDS:
        print(f"未知策略 id: {strategy_id}")
        sys.exit(1)
    fn = next(fn for sid, _, fn in se.BUILTIN_REGISTRY if sid == strategy_id)
    params = _params_of(strategy_id, se.get_strategies())

    df = se.get_daily_data(code, days=days)
    if len(df) < 60:
        print(f"{code} 历史数据不足({len(df)} 条)")
        sys.exit(1)
    close = df["close"]

    cash = 1.0
    in_pos = False
    entry = 0.0
    trades = []  # 每笔收益率
    warmup = 60
    for i in range(warmup, len(df) - 1):
        ctx = {
            "i": i,
            "price": float(close.iloc[i]),
            "df": df,
            "close": close,
            "code": code,
            "realtime": None,
        }
        try:
            sig, _ = fn(ctx, params)
        except Exception:
            continue
        nxt_open = float(df["open"].iloc[i + 1])
        if not in_pos and sig == "buy" and nxt_open > 0:
            in_pos = True
            entry = nxt_open
        elif in_pos and sig == "sell" and nxt_open > 0:
            cash *= nxt_open / entry
            trades.append(nxt_open / entry - 1)
            in_pos = False
    # 期末仍有持仓,按最后收盘价平仓
    if in_pos:
        last_close = float(close.iloc[-1])
        cash *= last_close / entry
        trades.append(last_close / entry - 1)

    total_ret = cash - 1
    bh_ret = float(close.iloc[-1]) / float(close.iloc[warmup]) - 1
    wins = sum(1 for t in trades if t > 0)
    print(f"\n策略 {strategy_id} × {code} 回测(近 {len(df) - warmup} 个交易日)")
    print(f"  交易次数:   {len(trades)}")
    print(f"  胜率:       {wins / len(trades) * 100:.0f}%" if trades else "  胜率:     -")
    print(f"  策略收益:   {total_ret * 100:+.1f}%")
    print(f"  买入持有:   {bh_ret * 100:+.1f}%")
    print(f"  超额收益:   {(total_ret - bh_ret) * 100:+.1f}%")


def cmd_scan(strategy_id, codes=None, top_n=20):
    if strategy_id not in se.BUILTIN_STRATEGY_IDS:
        print(f"未知策略 id: {strategy_id}")
        sys.exit(1)
    if strategy_id in se.NO_SCAN_STRATEGIES:
        print(f"策略 {strategy_id} 需联网获取外部数据,不适合扫描")
        sys.exit(1)
    if strategy_id in se.NO_BUY_SIGNAL_STRATEGIES:
        print(f"策略 {strategy_id} 为观察型(只输出观察信息),不适合选股扫描")
        sys.exit(1)

    if codes:
        # 显式指定股票:逐只拉取数据后运行策略
        strategies_cfg = se.get_strategies()
        fn = next(fn for sid, _, fn in se.BUILTIN_REGISTRY if sid == strategy_id)
        params = _params_of(strategy_id, strategies_cfg)
        hits = []
        for code in codes:
            df = se.get_daily_data(code)
            if len(df) < 60:
                print(f"  {code} 数据不足,跳过")
                continue
            close = df["close"]
            i = len(df) - 1
            ctx = {
                "i": i,
                "price": float(close.iloc[i]),
                "df": df,
                "close": close,
                "code": code,
                "realtime": None,
            }
            try:
                sg, reason = fn(ctx, params)
            except Exception:
                continue
            if sg == "buy":
                prev = float(close.iloc[i - 1])
                pct = (float(close.iloc[i]) - prev) / prev * 100 if prev > 0 else 0
                hits.append((code, float(close.iloc[i]), pct, reason))
        print(f"\n策略 {strategy_id} 扫描 {len(codes)} 只,命中 {len(hits)} 只:")
        for code, price, pct, reason in hits:
            print(f"  {code}  现价 {price:.2f} ({pct:+.2f}%)\n    {reason}")
        return

    res = se.scan_with_strategy(strategy_id, top_n=top_n)
    if "error" in res:
        print(res["error"])
        return
    print(f"\n策略 {strategy_id} 扫描 {res['scanned']} 只,命中 {res['hits_count']} 只:")
    for h in res["hits"]:
        print(f"  {h['code']}  现价 {h['price']:.2f} ({h['pct']:+.2f}%)\n    {h['reason']}")


def cmd_library(keyword=None):
    import json

    lib_path = Path(__file__).parent / "strategy_library.json"
    if not lib_path.exists():
        print("策略大全数据不存在")
        sys.exit(1)
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    found = False
    for src in lib.get("sources", []):
        for cat in src.get("categories", []):
            for st in cat.get("strategies", []):
                blob = json.dumps(st, ensure_ascii=False)
                if keyword and keyword.lower() not in blob.lower():
                    continue
                found = True
                impl = "已实现" if st.get("implemented") else "未实现"
                print(f"  [{src['id']}/{cat['name']}] {st.get('name', '')} ({impl})")
                desc = st.get("desc", "")
                if desc:
                    print(f"    {desc}")
    if not found:
        print("未找到匹配的策略" if keyword else "策略大全为空")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd, *args = sys.argv[1:]

    if cmd == "analyze":
        if not args:
            print("用法: cli.py analyze <code> [<code> ...]")
            sys.exit(1)
        cmd_analyze(args)
    elif cmd == "list":
        cmd_list()
    elif cmd == "toggle":
        if len(args) != 2 or args[1] not in ("on", "off"):
            print("用法: cli.py toggle <strategy_id> <on|off>")
            sys.exit(1)
        cmd_toggle(args[0], args[1] == "on")
    elif cmd == "params":
        if len(args) < 2:
            print("用法: cli.py params <strategy_id> <k=v> [<k=v> ...]")
            sys.exit(1)
        cmd_params(args[0], args[1:])
    elif cmd == "backtest":
        if len(args) < 2:
            print("用法: cli.py backtest <strategy_id> <code> [--days N]")
            sys.exit(1)
        days = 320
        if "--days" in args:
            i = args.index("--days")
            days = int(args[i + 1])
            args = [a for j, a in enumerate(args) if j != i and j != i + 1]
        cmd_backtest(args[0], args[1], days=days)
    elif cmd == "scan":
        if not args:
            print("用法: cli.py scan <strategy_id> [--codes c1,c2,...] [--top N]")
            sys.exit(1)
        codes = None
        top_n = 20
        rest = args[1:]
        if "--codes" in rest:
            i = rest.index("--codes")
            codes = [c for c in rest[i + 1].split(",") if c]
            rest = rest[:i]
        if "--top" in rest:
            i = rest.index("--top")
            top_n = int(rest[i + 1])
        cmd_scan(args[0], codes=codes, top_n=top_n)
    elif cmd == "library":
        cmd_library(args[0] if args else None)
    else:
        print(f"未知子命令: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
