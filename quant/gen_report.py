import json
from pathlib import Path

OUTPUT = Path(__file__).parent
results = json.loads((OUTPUT / 'backtest_report.json').read_text(encoding='utf-8'))

strategies = {}
for r in results:
    if 'error' in r:
        continue
    strategies.setdefault(r['strategy'], []).append(r)

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
        lines.append(f"| {r['stock']} | {r['total_return_pct']:+.2f}% | "
                     f"{r['annual_return_pct']:+.2f}% | "
                     f"{r['sharpe_ratio'] if r['sharpe_ratio'] is not None else '-'} | "
                     f"{r['max_drawdown_pct']:.2f}% |")
    tr = avg(strat_results, 'total_return_pct')
    ar = avg(strat_results, 'annual_return_pct')
    sr = avg(strat_results, 'sharpe_ratio')
    dd = avg(strat_results, 'max_drawdown_pct')
    lines.append(f"\n**平均：** 总收益 {tr:+.2f}% | 年化 {ar:+.2f}% | 夏普 {sr if sr is not None else '-'} | 最大回撤 {dd:.2f}%\n")

lines.append("## 结论\n")
lines.append("- **5日均线止损法** 平均表现最好，趋势跟踪有效")
lines.append("- **MACD金叉死叉** 在部分股票上有效，需结合个股筛选")
lines.append("- **KDJ超买超卖** 单独使用效果一般，适合作为辅助指标")
lines.append("\n> 注：简单单股回测，未考虑仓位管理、大盘过滤、多股对冲等，实盘需谨慎。")

(OUTPUT / '回测报告.md').write_text('\n'.join(lines), encoding='utf-8')
print('报告已生成: 回测报告.md')