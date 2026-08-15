import backtrader as bt
from pathlib import Path
from datetime import datetime
from data_fetcher import get_backtrader_data
from strategies import (
    MACDStrategy, KDJStrategy, MAStopStrategy,
    BOLLStrategy, DMIStrategy, PSYStrategy, BIASStrategy, SARStrategy,
    MACDKDJBOLLStrategy, MACombinationStrategy, VolumePriceDivergenceStrategy,
)

STOCKS = [
    "600789", "000001", "002446", "300750", "600519",
    "000858", "002415", "600036", "601318", "000333",
]
START = "20220601"
END = "20240801"
CASH = 100000.0
COMMISSION = 0.00025

def run_single_strategy(strategy_cls, name, stock, params=None):
    cerebro = bt.Cerebro()
    cerebro.broker.setcash(CASH)
    cerebro.broker.setcommission(commission=COMMISSION)

    if params:
        cerebro.addstrategy(strategy_cls, **params)
    else:
        cerebro.addstrategy(strategy_cls)

    try:
        df = get_backtrader_data(stock, START, END)
        if len(df) < 100:
            return None
        data = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data)
        cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.02, annualize=True)
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")

        results = cerebro.run()
        strat = results[0]
        ret = strat.analyzers.returns.get_analysis()
        sharpe = strat.analyzers.sharpe.get_analysis()
        dd = strat.analyzers.drawdown.get_analysis()

        total_return = ret.get("rtot", 0) * 100
        ann_return = ret.get("rnorm100", 0)
        sharpe_ratio = sharpe.get("sharperatio", None)
        max_dd = dd.get("max", {}).get("drawdown", 0) if isinstance(dd.get("max"), dict) else dd.get("max", 0)
        final_value = cerebro.broker.getvalue()

        return {
            "stock": stock,
            "strategy": name,
            "total_return_pct": round(total_return, 2),
            "annual_return_pct": round(ann_return, 2),
            "sharpe_ratio": round(sharpe_ratio, 2) if sharpe_ratio else None,
            "max_drawdown_pct": round(max_dd, 2),
            "final_value": round(final_value, 2),
        }
    except Exception as e:
        return {"stock": stock, "strategy": name, "error": str(e)[:200]}


def main():
    print("=" * 80)
    print("A股量化策略回测系统")
    print(f"回测区间: {START} ~ {END}")
    print(f"初始资金: {CASH:,.0f} 元")
    print(f"佣金: {COMMISSION*100:.3f}%")
    print("=" * 80)

    strategies = [
        (MACDStrategy, "MACD金叉死叉"),
        (KDJStrategy, "KDJ超买超卖"),
        (MAStopStrategy, "5日均线止损"),
        (BOLLStrategy, "BOLL布林线"),
        (DMIStrategy, "DMI趋势"),
        (PSYStrategy, "PSY心理线"),
        (BIASStrategy, "乖离率BIAS"),
        (SARStrategy, "SAR止损"),
        (MACDKDJBOLLStrategy, "三指标共振"),
        (MACombinationStrategy, "均线组合"),
        (VolumePriceDivergenceStrategy, "量价背离"),
    ]

    all_results = []
    for strategy_cls, name in strategies:
        print(f"\n{'='*80}")
        print(f"策略: {name}")
        print(f"{'='*80}")
        for stock in STOCKS:
            result = run_single_strategy(strategy_cls, name, stock)
            if result:
                if "error" in result:
                    print(f"  {stock}: {result['error']}")
                else:
                    print(f"  {stock}: 总收益{result['total_return_pct']:+.2f}% | "
                          f"年化{result['annual_return_pct']:+.2f}% | "
                          f"夏普{result['sharpe_ratio']} | "
                          f"回撤{result['max_drawdown_pct']:.2f}%")
                    all_results.append(result)

    print(f"\n{'='*80}")
    print("汇总结果")
    print(f"{'='*80}")
    if all_results:
        for r in all_results:
            if "error" not in r:
                print(f"  {r['strategy']:12s} | {r['stock']} | "
                      f"收益{r['total_return_pct']:>+7.2f}% | "
                      f"年化{r['annual_return_pct']:>+6.2f}% | "
                      f"夏普{r['sharpe_ratio']} | "
                      f"回撤{r['max_drawdown_pct']:>5.2f}%")

    import json
    report_path = Path(__file__).parent / "backtest_report.json"
    report_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已保存: {report_path}")


if __name__ == "__main__":
    main()