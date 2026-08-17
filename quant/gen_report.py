import json
from pathlib import Path

OUTPUT = Path(__file__).parent
results = json.loads((OUTPUT / "backtest_report.json").read_text(encoding="utf-8"))

strategies = {}
for r in results:
    if "error" in r:
        continue
    strategies.setdefault(r["strategy"], []).append(r)

strategy_avgs = []


def avg(lst, key):
    vals = [x[key] for x in lst if x.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


lines = []
lines.append("# A股量化策略回测报告\n")
lines.append("> 数据源：新浪财经 | 回测框架：Backtrader\n")
lines.append("## 回测参数\n")
lines.append("- 区间：2022-06-01 ~ 2024-08-01")
lines.append("- 初始资金：100,000 元")
lines.append("- 佣金：0.025%")
lines.append("- 股票池：10 只代表股票\n")

for strat_name, strat_results in strategies.items():
    lines.append(f"## {strat_name}\n")
    lines.append("| 股票 | 总收益率 | 年化收益率 | 夏普比率 | 最大回撤 |")
    lines.append("|------|---------|-----------|---------|---------|")
    for r in strat_results:
        lines.append(
            f"| {r['stock']} | {r['total_return_pct']:+.2f}% | "
            f"{r['annual_return_pct']:+.2f}% | "
            f"{r['sharpe_ratio'] if r['sharpe_ratio'] is not None else '-'} | "
            f"{r['max_drawdown_pct']:.2f}% |"
        )
    avg_total = avg(strat_results, "total_return_pct")
    if avg_total is not None:
        strategy_avgs.append(
            (
                strat_name,
                avg_total,
                avg(strat_results, "annual_return_pct"),
                avg(strat_results, "sharpe_ratio"),
                avg(strat_results, "max_drawdown_pct"),
            )
        )

lines.append("## 结论\n")
# 按平均总收益排序，动态挑出最优/最差
strategy_avgs.sort(key=lambda x: x[1] or 0, reverse=True)
if strategy_avgs:
    best = strategy_avgs[0]
    worst = strategy_avgs[-1]
    lines.append(f"- **{best[0]}** 平均表现最好，总收益 {best[1]:+.2f}%")
    if best[3] is not None:
        lines.append(f"  - 年化 {best[2]:+.2f}% | 夏普 {best[3]} | 最大回撤 {best[4]:.2f}%")
    if worst[0] != best[0]:
        lines.append(f"- **{worst[0]}** 表现最弱，总收益 {worst[1]:+.2f}%，建议谨慎或作为反向信号")
    lines.append("- 各策略在不同股票上分化明显，实盘需结合个股筛选与大盘环境")
lines.append("\n> 注：简单单股回测，未考虑仓位管理、大盘过滤、多股对冲等，实盘需谨慎。")

(OUTPUT / "回测报告.md").write_text("\n".join(lines), encoding="utf-8")
print("报告已生成: 回测报告.md")
