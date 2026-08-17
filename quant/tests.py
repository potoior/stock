"""完整测试脚本：测试所有 Agent 模块 + API 接口"""

import json
import time

failed = 0


def test(name, ok, detail=""):
    global failed
    if ok:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}: {detail}")
        failed += 1


print("=" * 60)
print("量化系统完整测试")
print("=" * 60)

# 1. 测试 portfolio
print("\n1. Portfolio 持仓管理")
from portfolio import Portfolio

p = Portfolio(100000)
p.reset()
test("初始化", p.cash == 100000 and len(p.positions) == 0)
ok, msg = p.buy("600789", "鲁抗医药", 10.0, 1000)
test("买入", ok, msg)
test("持仓数", len(p.positions) == 1)
test("现金减少", p.cash < 100000)
ok, msg = p.sell("600789", "鲁抗医药", 11.0, 1000)
test("卖出", ok, msg)
test("平仓后无持仓", len(p.positions) == 0)
test("现金增加", p.cash > 100000)
p.reset()
test("重置", p.cash == 100000)

# 2. 测试 risk_manager
print("\n2. Risk Manager 风险控制")
from risk_manager import RiskManager

rm = RiskManager()
test("初始化", rm.config["max_single_position"] == 0.20)
today = time.strftime("%Y-%m-%d")
rm.reset_daily(today)
ok, _, _ = rm.check_buy("600789", 10.0, p, 0, False)
test("允许买入", ok)
rm.record_trade()
test("交易次数记录", rm.daily_trades == 1)
sell, reason = rm.check_sell(
    "600789", 9.0, type("obj", (object,), {"positions": {"600789": {"cost": 10.0}}})()
)
test("止损触发", sell and "止损" in reason)

# 3. 测试 executor
print("\n3. Executor 交易执行")
from executor import SimExecutor

ex = SimExecutor(100000)
ex.portfolio.reset()
quotes = [{"code": "600789", "name": "鲁抗医药", "price": 10.0, "pct": 0.5}]
prices = {"600789": 10.0}
res = ex.execute("600789", "鲁抗医药", 10.0, "buy", 0.5)
test("执行买入", res["status"] == "executed", res.get("msg"))
test("买入后现金减少", ex.portfolio.cash < 100000, f"现金 {ex.portfolio.cash:.2f}")
test("买入后持仓1只", len(ex.portfolio.positions) == 1)
res = ex.execute("600789", "鲁抗医药", 11.0, "sell")
test("执行卖出", res["status"] == "executed", res.get("msg"))
summary = ex.get_summary({})
test("卖出后盈利", summary["total_value"] > 100000, f"当前 {summary['total_value']:.2f}")

# 4. 测试 feedback
print("\n4. Feedback 自我反馈")
from feedback import Feedback

fb = Feedback()
analysis = fb.analyze()
test("反馈分析正常", "recent_win_rate" in analysis)
test("有交易记录", analysis["recent_trades"] > 0, str(analysis["recent_trades"]))
test("有建议", len(analysis["suggestions"]) > 0)

# 5. 测试 API
print("\n5. API 接口")
import urllib.request

try:
    resp = urllib.request.urlopen("http://127.0.0.1:8000/api/quotes", timeout=10)
    data = json.loads(resp.read())
    test("API 行情接口", "data" in data and len(data["data"]) > 0, f"{len(data['data'])} 条")
except Exception as e:
    test("API 行情接口", False, str(e))

try:
    resp = urllib.request.urlopen("http://127.0.0.1:8000/api/kline/600789", timeout=10)
    data = json.loads(resp.read())
    test("API K线接口", "data" in data and len(data["data"]) > 0, f"{len(data['data'])} 条")
except Exception as e:
    test("API K线接口", False, str(e))

# 6. 测试 agent
print("\n6. Agent 主循环")
from agent import TradingAgent

agent = TradingAgent(codes=["600789"], interval=5)
agent.run(max_cycles=1)
agent.feedback.close()
test("Agent 运行", agent.cycle_count == 1)

# 7. 总结
print("\n" + "=" * 60)
if failed == 0:
    print("全部测试通过 ✓")
else:
    print(f"{failed} 个测试失败 ✗")
print("=" * 60)
