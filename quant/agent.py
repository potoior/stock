import time
import json
import sys
from datetime import datetime
from pathlib import Path
from executor import SimExecutor
from feedback import Feedback
from data_fetcher import fetch_realtime, get_daily_data

# 默认监控股票池
DEFAULT_CODES = ["600789", "000001", "600519", "601318", "000333", "002415"]

class TradingAgent:
    def __init__(self, codes=None, interval=60, mode="simulation"):
        self.codes = codes or DEFAULT_CODES
        self.interval = interval
        self.mode = mode
        self.executor = SimExecutor()
        self.feedback = Feedback()
        self.cycle_count = 0

    def compute_signals(self, code, price):
        try:
            df = get_daily_data(code, "20240101")
            if len(df) < 60:
                return {}
            close = df["close"].astype(float)
            high = df["high"].astype(float)
            low = df["low"].astype(float)
            ma5 = close.rolling(5).mean().iloc[-1]
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd_bull = dif.iloc[-1] > dea.iloc[-1]
            low9 = low.rolling(9).min()
            high9 = high.rolling(9).max()
            rsv = (close - low9) / (high9 - low9) * 100
            k = rsv.ewm(com=2, adjust=False).mean().iloc[-1]
            return {
                "ma5": round(ma5, 2),
                "macd_bull": bool(macd_bull),
                "k": round(k, 1),
                "above_ma5": price > ma5,
                "score": (1 if price > ma5 else 0) + (1 if macd_bull else 0),
            }
        except:
            return {}

    def scan(self):
        quotes = fetch_realtime(self.codes)
        prices = {q["code"]: q["price"] for q in quotes}
        open_ok, msg = self.executor.is_market_open()
        market_pct = None
        if len(quotes) > 0:
            ref = quotes[0]
            market_pct = ref["pct"]

        for q in quotes:
            code = q["code"]
            price = q["price"]
            sig = self.compute_signals(code, price)
            if not sig:
                continue

            position = self.executor.portfolio.positions.get(code)

            # 卖出检查（止损/止盈）
            if position:
                stop, reason = self.executor.risk.check_sell(code, price, self.executor.portfolio)
                if stop:
                    res = self.executor.execute(code, q["name"], price, "sell", market_pct)
                    if res["status"] == "executed":
                        print(f"  {q['name']} 止损/止盈卖出: {reason}")

            # 买入信号
            if not position and open_ok and sig.get("score", 0) >= 2:
                res = self.executor.execute(code, q["name"], price, "buy", market_pct)
                if res["status"] == "executed":
                    print(f"  {q['name']} 买入: {res['msg']}")

        summary = self.executor.get_summary(prices)
        return {"quotes": quotes, "summary": summary, "open": open_ok, "msg": msg}

    def cycle(self):
        self.cycle_count += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] 第 {self.cycle_count} 次扫描")
        result = self.scan()
        s = result["summary"]
        print(f"  总资产: {s['total_value']:.2f} | 现金: {s['cash']:.2f} | "
              f"持仓: {s['positions']} 只 | 总收益: {s['total_return']:+.2f} ({s['return_pct']:+.2f}%)")

    def run(self, max_cycles=None):
        print(f"交易 Agent 启动 [模式: {self.mode}]")
        print(f"监控股票: {', '.join(self.codes)}")
        print(f"扫描间隔: {self.interval} 秒")
        print("-" * 60)
        while True:
            try:
                self.cycle()
                if max_cycles and self.cycle_count >= max_cycles:
                    break
                time.sleep(self.interval)
            except KeyboardInterrupt:
                print("\nAgent 停止")
                break
            except Exception as e:
                print(f"  错误: {e}")
                time.sleep(max(5, self.interval))

    def report(self):
        print("\n" + "=" * 60)
        print("交易 Agent 诊断报告")
        print("=" * 60)
        analysis = self.feedback.analyze()
        print(f"近期胜率: {analysis['recent_win_rate']:.1%} ({analysis['recent_trades']} 笔)")
        print(f"盈亏比:   {analysis['profit_loss_ratio']:.2f}")
        if analysis["alerts"]:
            print("\n⚠️  告警:")
            for a in analysis["alerts"]:
                print(f"  - {a}")
        if analysis["suggestions"]:
            print("\n💡 建议:")
            for s in analysis["suggestions"]:
                print(f"  - {s}")
        print("\n最近交易:")
        for t in analysis["last_trades"][:10]:
            pnl = f"盈亏 {t['pnl']:+.2f}" if t["pnl"] else ""
            print(f"  {t['date']} {t['code']} {t['action']} {t['price']:.2f} {t['qty']}股 {pnl}")

if __name__ == "__main__":
    codes = sys.argv[1:] if len(sys.argv) > 1 else None
    agent = TradingAgent(codes, interval=60)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    finally:
        agent.report()
        agent.feedback.close()