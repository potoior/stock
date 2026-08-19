"""工具调用统计 CLI: 分析 /tmp/feishu_bot_audit.jsonl 审计日志。

用法:
  python tool_stats.py                    # 总览(默认 24 小时)
  python tool_stats.py --hours 1          # 最近 1 小时
  python tool_stats.py --session sessA    # 按会话过滤
  python tool_stats.py --tool analyze_stock  # 按工具过滤
  python tool_stats.py --errors           # 只看错误
  python tool_stats.py --tail 20          # 最近 20 条明细
  python tool_stats.py --since 2026-08-19 # 指定起始日期

输出维度:
  1. 总览: 总调用数/错误数/错误率/平均耗时
  2. 按工具: 调用次数 / 平均耗时 / 错误率
  3. 按会话: Top 10 活跃会话
  4. 错误明细(可选)
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOG = Path("/tmp/feishu_bot_audit.jsonl")


def load_records(log_path: Path) -> list[dict]:
    """读取 JSONL 审计日志,跳过坏行。"""
    if not log_path.exists():
        return []
    out = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def parse_ts(rec: dict) -> datetime | None:
    ts = rec.get("ts")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def filter_records(records: list[dict], hours: float | None,
                   since: str | None, session: str | None,
                   tool: str | None, errors_only: bool) -> list[dict]:
    out = []
    cutoff = (datetime.now() - timedelta(hours=hours)) if hours else None
    since_dt = datetime.fromisoformat(since) if since else None
    for r in records:
        ts = parse_ts(r)
        if cutoff and (not ts or ts < cutoff):
            continue
        if since_dt and (not ts or ts < since_dt):
            continue
        if session and r.get("session_id") != session:
            continue
        if tool and r.get("tool") != tool:
            continue
        if errors_only and not r.get("error"):
            continue
        out.append(r)
    return out


def print_overview(records: list[dict]) -> None:
    if not records:
        print("无数据")
        return
    total = len(records)
    errs = sum(1 for r in records if r.get("error"))
    durs = [r.get("duration_ms", 0) for r in records if r.get("duration_ms")]
    avg_dur = sum(durs) / len(durs) if durs else 0
    p95_dur = sorted(durs)[int(len(durs) * 0.95)] if durs else 0
    first = parse_ts(records[0])
    last = parse_ts(records[-1])
    print("=== 总览 ===")
    print(f"  调用数: {total}")
    print(f"  错误数: {errs} ({errs/total*100:.1f}%)")
    print(f"  平均耗时: {avg_dur:.0f}ms / P95: {p95_dur}ms")
    if first and last:
        print(f"  时间范围: {first:%Y-%m-%d %H:%M} ~ {last:%Y-%m-%d %H:%M}")


def print_by_tool(records: list[dict]) -> None:
    if not records:
        return
    counter = Counter(r.get("tool", "?") for r in records)
    err_counter = Counter(r.get("tool", "?") for r in records if r.get("error"))
    dur_sum = defaultdict(int)
    dur_cnt = defaultdict(int)
    for r in records:
        t = r.get("tool", "?")
        d = r.get("duration_ms", 0) or 0
        dur_sum[t] += d
        dur_cnt[t] += 1
    print("\n=== 按工具 ===")
    print(f"{'工具':<28} {'次数':>6} {'错误':>6} {'平均耗时':>10}")
    print("-" * 56)
    for t, n in counter.most_common():
        err = err_counter.get(t, 0)
        avg = dur_sum[t] / dur_cnt[t] if dur_cnt[t] else 0
        print(f"{t:<28} {n:>6} {err:>6} {avg:>8.0f}ms")


def print_by_session(records: list[dict], top_n: int = 10) -> None:
    if not records:
        return
    counter = Counter(r.get("session_id", "?") for r in records)
    print(f"\n=== 按会话 Top {top_n} ===")
    print(f"{'会话 ID':<50} {'调用数':>8}")
    print("-" * 60)
    for s, n in counter.most_common(top_n):
        # 会话 ID 可能很长,截断显示
        s_show = s if len(s) <= 48 else s[:45] + "..."
        print(f"{s_show:<50} {n:>8}")


def print_errors(records: list[dict], limit: int = 20) -> None:
    errs = [r for r in records if r.get("error")]
    if not errs:
        return
    print(f"\n=== 错误明细 (最近 {min(limit, len(errs))} 条) ===")
    for r in errs[-limit:]:
        ts = r.get("ts", "?")
        t = r.get("tool", "?")
        err = r.get("error", "")
        # 错误信息截断
        err_show = err if len(err) <= 100 else err[:97] + "..."
        print(f"  [{ts}] {t}: {err_show}")


def print_tail(records: list[dict], limit: int = 20) -> None:
    if not records:
        return
    print(f"\n=== 最近 {min(limit, len(records))} 条明细 ===")
    print(f"{'时间':<20} {'工具':<22} {'耗时':>8} {'结果大小':>10} {'错误':>6}")
    print("-" * 70)
    for r in records[-limit:]:
        ts = r.get("ts", "?")[:19]
        t = r.get("tool", "?")
        dur = r.get("duration_ms", 0) or 0
        sz = r.get("result_size", 0) or 0
        err = "✗" if r.get("error") else ""
        print(f"{ts:<20} {t:<22} {dur:>6}ms {sz:>8}B {err:>6}")


def main():
    ap = argparse.ArgumentParser(description="工具调用审计日志统计")
    ap.add_argument("--log", default=str(DEFAULT_LOG), help=f"审计日志路径(默认 {DEFAULT_LOG})")
    ap.add_argument("--hours", type=float, default=24, help="最近 N 小时(0=全部,默认 24)")
    ap.add_argument("--since", default="", help="起始日期 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
    ap.add_argument("--session", default="", help="按会话 ID 过滤")
    ap.add_argument("--tool", default="", help="按工具名过滤")
    ap.add_argument("--errors", action="store_true", help="只看错误")
    ap.add_argument("--tail", type=int, default=0, help="显示最近 N 条明细")
    ap.add_argument("--no-overview", action="store_true", help="不显示总览")
    ap.add_argument("--no-by-tool", action="store_true", help="不显示按工具统计")
    ap.add_argument("--no-by-session", action="store_true", help="不显示按会话统计")
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"日志文件不存在: {log_path}", file=sys.stderr)
        print("提示: Bot 启动并处理消息后才会生成审计日志。", file=sys.stderr)
        sys.exit(1)

    records = load_records(log_path)
    if not records:
        print(f"日志为空: {log_path}", file=sys.stderr)
        sys.exit(0)

    filtered = filter_records(
        records,
        hours=args.hours if args.hours > 0 else None,
        since=args.since or None,
        session=args.session or None,
        tool=args.tool or None,
        errors_only=args.errors,
    )

    if not filtered:
        print("无匹配记录", file=sys.stderr)
        sys.exit(0)

    if not args.no_overview:
        print_overview(filtered)
    if not args.no_by_tool:
        print_by_tool(filtered)
    if not args.no_by_session:
        print_by_session(filtered)
    if args.errors:
        print_errors(filtered)
    if args.tail:
        print_tail(filtered, limit=args.tail)


if __name__ == "__main__":
    main()
