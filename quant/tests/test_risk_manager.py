"""RiskManager 风险控制单元测试"""

import time

from risk_manager import RiskManager


class FakePositions:
    def __init__(self, cost):
        self.positions = {"600789": {"cost": cost}}


def test_init_config():
    rm = RiskManager()
    assert rm.config["max_single_position"] == 0.20


def test_daily_reset_and_record():
    rm = RiskManager()
    rm.reset_daily(time.strftime("%Y-%m-%d"))
    assert rm.daily_trades == 0
    rm.record_trade()
    assert rm.daily_trades == 1


def test_check_buy_allowed():
    rm = RiskManager()
    p = type("P", (object,), {"positions": {}, "cash": 100000})()
    ok, _, _ = rm.check_buy("600789", 10.0, p, 0, False)
    assert ok


def test_check_sell_stoploss():
    rm = RiskManager()
    fp = FakePositions(cost=10.0)
    sell, reason = rm.check_sell("600789", 9.0, fp)
    assert sell
    assert "止损" in reason
