"""Portfolio 持仓管理单元测试"""

from portfolio import Portfolio


def test_init():
    p = Portfolio(100000)
    p.reset()
    assert p.cash == 100000
    assert len(p.positions) == 0


def test_buy_sell_roundtrip():
    p = Portfolio(100000)
    p.reset()
    ok, msg = p.buy("600789", "鲁抗医药", 10.0, 1000)
    assert ok, msg
    assert len(p.positions) == 1
    assert p.cash < 100000
    ok2, msg2 = p.sell("600789", "鲁抗医药", 11.0, 1000)
    assert ok2, msg2
    assert len(p.positions) == 0
    assert p.cash > 100000


def test_buy_insufficient_cash():
    p = Portfolio(100)
    p.reset()
    ok, msg = p.buy("600789", "鲁抗医药", 10.0, 1000)
    assert not ok
    assert "现金" in msg or "资金" in msg or "不足" in msg
