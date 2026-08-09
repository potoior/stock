import time
import json
import sys
from datetime import datetime
from executor import SimExecutor
from feedback import Feedback
from data_fetcher import fetch_realtime, get_daily_data

DEFAULT_CODES = ["600789", "000001", "600519", "601318", "000333", "002415"]

class TradingAgent:
    def __init__(self, codes=None, interval=60, mode="ai"):
        self.codes = codes or DEFAULT_CODES
        self.interval = interval
        self.mode = mode
        self.executor = SimExecutor()
        self.feedback = Feedback()
        self.cycle_count = 0
        self.ai_decider = None
        if mode in ("ai", "hybrid"):
            from ai_decider import AIDecider
            self.ai_decider = AIDecider()

    def compute_signals(self, code, price):
        try:
            df = get_daily_data(code, "20240101")
            if len(df) < 60:
                return {}
            close = df["close"].astype(float)
            high = df["high"].astype(float)
            low = df["low"].astype(float)

            if price is not None:
                import pandas as pd
                close = pd.concat([close[:-1], pd.Series([price])], ignore_index=True)
                high = pd.concat([high[:-1], pd.Series([max(high.iloc[-1], price)])], ignore_index=True)
                low = pd.concat([low[:-1], pd.Series([min(low.iloc[-1], price)])], ignore_index=True)

            ma5 = close.rolling(5).mean().iloc[-1]
            ma10 = close.rolling(10).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd_bull = dif.iloc[-1] > dea.iloc[-1]
            low9 = low.rolling(9).min()
            high9 = high.rolling(9).max()
            rsv = (close - low9) / (high9 - low9) * 100
            k = rsv.ewm(com=2, adjust=False).mean().iloc[-1]
            d = k  # 简化: 用前一条D值
            return {
                "ma5": round(ma5, 2), "ma10": round(ma10, 2), "ma20": round(ma20, 2),
                "macd_bull": bool(macd_bull),
                "k": round(k, 1),
                "kdj_signal": "超卖" if k < 20 else ("超买" if k > 80 else "中性"),
                "above_ma5": price > ma5,
                "score": (1 if price > ma5 else 0) + (1 if macd_bull else 0),
            }
        except:
            return {}

    def scan(self):
        quotes = fetch_realtime(self.codes)
        prices = {q["code"]: q["price"] for q in quotes}
        open_ok, msg = self.executor.is_market_open()

        # 计算所有信号
        for q in quotes:
            sig = self.compute_signals(q["code"], q["price"])
            if sig:
                q.update(sig)

        market_data = {"quotes": quotes, "time": datetime.now().isoformat()}

        if self.mode == "ai" and self.ai_decider:
            self._ai_cycle(market_data)
        else:
            self._rule_cycle(quotes, prices)

        summary = self.executor.get_summary(prices)
        return {"quotes": quotes, "summary": summary, "open": open_ok, "msg": msg}

    def _rule_cycle(self, quotes, prices):
        """规则模式：MACD/KDJ/MA5 规则判断"""
        for q in quotes:
            sig = self.compute_signals(q["code"], q["price"])
            if not sig:
                continue
            position = self.executor.portfolio.positions.get(q["code"])
            if position:
                stop, reason = self.executor.risk.check_sell(
                    q["code"], q["price"], self.executor.portfolio
                )
                if stop:
                    res = self.executor.execute(q["code"], q["name"], q["price"], "sell")
                    if res["status"] == "executed":
                        print(f"  {q['name']} 规则止损: {reason}")
            if not position and sig.get("score", 0) >= 2:
                open_ok, _ = self.executor.is_market_open()
                if open_ok:
                    res = self.executor.execute(q["code"], q["name"], q["price"], "buy", q.get("pct"))
                    if res["status"] == "executed":
                        print(f"  {q['name']} 规则买入")

    def _ai_cycle(self, market_data):
        """AI模式：AI做决策，风控做约束"""
        try:
            decision = self.ai_decider.decide(market_data, self.executor.portfolio)
            actions = decision.get("actions", [])
            judgment = decision.get("market_judgment", "未知")
            risk = decision.get("risk_level", "中")
            print(f"  AI判断: {judgment} | 风险: {risk} | 建议操作: {len(actions)} 条", flush=True)

            for act in actions:
                code = act.get("code", "")
                action = act.get("action", "hold")
                reason = act.get("reason", "")
                quote = next((q for q in market_data["quotes"] if q["code"] == code), None)
                if not quote:
                    continue

                if action == "buy":
                    position = self.executor.portfolio.positions.get(code)
                    if position:
                        continue
                    open_ok, _ = self.executor.is_market_open()
                    if not open_ok:
                        continue
                    # 风控检查
                    ok, _, rsn = self.executor.risk.check_buy(
                        code, quote["price"], self.executor.portfolio,
                        quote.get("pct"), False
                    )
                    if not ok:
                        print(f"  {code} AI建议买入但风控拒绝: {rsn}")
                        continue
                    res = self.executor.execute(code, quote["name"], quote["price"], "buy", quote.get("pct"))
                    if res["status"] == "executed":
                        print(f"  {quote['name']} AI买入: {reason}")

                elif action == "sell":
                    position = self.executor.portfolio.positions.get(code)
                    if not position:
                        continue
                    # 止损/止盈风控
                    stop, rsn = self.executor.risk.check_sell(
                        code, quote["price"], self.executor.portfolio
                    )
                    if stop:
                        reason = f"{reason} (风控触发: {rsn})"
                    res = self.executor.execute(code, quote["name"], quote["price"], "sell")
                    if res["status"] == "executed":
                        print(f"  {quote['name']} AI卖出: {reason}")

        except Exception as e:
            print(f"  AI决策异常: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def cycle(self):
        self.cycle_count += 1
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] 第 {self.cycle_count} 次扫描 [模式: {self.mode}]", flush=True)
        result = self.scan()
        s = result["summary"]
        print(f"  总资产: {s['total_value']:.2f} | 现金: {s['cash']:.2f} | "
              f"持仓: {s['positions']} 只 | 收益: {s['total_return']:+.2f} ({s['return_pct']:+.2f}%)", flush=True)

    def run(self, max_cycles=None):
        print(f"交易 Agent 启动 [模式: {self.mode}]", flush=True)
        print(f"监控股票: {', '.join(self.codes)}", flush=True)
        print(f"扫描间隔: {self.interval} 秒", flush=True)
        print("-" * 60, flush=True)
        while True:
            try:
                self.cycle()
                if max_cycles and self.cycle_count >= max_cycles:
                    break
                time.sleep(self.interval)
            except KeyboardInterrupt:
                print("\nAgent 停止", flush=True)
                break
            except Exception as e:
                print(f"  错误: {e}", flush=True)
                time.sleep(max(5, self.interval))

    def report(self):
        print("\n" + "=" * 60, flush=True)
        print("Agent 诊断报告", flush=True)
        print("=" * 60, flush=True)
        analysis = self.feedback.analyze()
        print(f"近期胜率: {analysis['recent_win_rate']:.1%} ({analysis['recent_trades']} 笔)", flush=True)
        print(f"盈亏比:   {analysis['profit_loss_ratio']:.2f}", flush=True)
        if analysis["alerts"]:
            print("\n告警:", flush=True)
            for a in analysis["alerts"]:
                print(f"  - {a}", flush=True)
        if analysis["suggestions"]:
            print("\n建议:", flush=True)
            for s in analysis["suggestions"]:
                print(f"  - {s}", flush=True)

if __name__ == "__main__":
    mode = "ai"
    codes = sys.argv[1:] if len(sys.argv) > 1 else None
    if "--rule" in sys.argv:
        mode = "rule"
        sys.argv = [a for a in sys.argv if a != "--rule"]
    agent = TradingAgent(codes, interval=60, mode=mode)
    try:
        agent.run()
    except KeyboardInterrupt:
        pass
    finally:
        agent.report()
        agent.feedback.close()