"""daily_scan.market_stats 涨停/跌停统计 + strategy_engine.limit_prices 测试。"""

import strategy_engine as se
from daily_scan import market_stats

# ---------------- limit_prices ----------------


def test_limit_prices_main_board():
    """主板 10%:10.00 → 涨停 11.00,跌停 9.00。"""
    zt, zd = se.limit_prices("600519", 10.00)
    assert zt == 11.00
    assert zd == 9.00


def test_limit_prices_gem_board():
    """创业板 20%:10.00 → 涨停 12.00,跌停 8.00。"""
    zt, zd = se.limit_prices("300750", 10.00)
    assert zt == 12.00
    assert zd == 8.00


def test_limit_prices_st_main_board():
    """主板 ST 5%:10.00 → 涨停 10.50。"""
    zt, zd = se.limit_prices("600000", 10.00, st=True)
    assert zt == 10.50
    assert zd == 9.50


def test_limit_prices_st_gem_board_stays_20pct():
    """创业板 ST 仍为 20%:300xxx ST 涨停价应按 20% 计算。"""
    zt, zd = se.limit_prices("300001", 10.00, st=True)
    assert zt == 12.00
    assert zd == 8.00


def test_limit_prices_round_half_up():
    """四舍五入到分:10.05×1.1=11.055 应为 11.06(银行家舍入会错成 11.05)。"""
    zt, _ = se.limit_prices("600000", 10.05)
    assert zt == 11.06


# ---------------- market_stats ----------------


def _row(code, name, trade, pct):
    return {"code6": code, "name": name, "trade": trade, "changepercent": pct, "amount": 1e8}


def test_market_stats_main_board_limit_up():
    rows = [_row("600519", "贵州茅台", 11.00, 10.0)]
    s = market_stats(rows)
    assert s["limit_up"] == 1 and s["limit_down"] == 0
    assert s["up"] == 1


def test_market_stats_low_price_limit_up():
    """低价股涨停:2.44 → 2.68(涨 9.84%),固定 9.9 阈值会漏计。"""
    rows = [_row("600001", "低价股", 2.68, 9.84)]
    s = market_stats(rows)
    assert s["limit_up"] == 1


def test_market_stats_near_limit_not_counted():
    """+9.9% 未封板不算涨停。"""
    rows = [_row("600001", "普通股", 10.99, 9.9)]
    s = market_stats(rows)
    assert s["limit_up"] == 0


def test_market_stats_st_limit_up():
    """主板 ST 5% 板:10.00 → 10.50 算涨停。"""
    rows = [_row("600002", "ST测试", 10.50, 5.0)]
    s = market_stats(rows)
    assert s["limit_up"] == 1


def test_market_stats_gem_st_limit_up():
    """创业板 ST 20% 板:10.00 → 12.00 算涨停,10.50(+5%)不算。"""
    rows = [_row("300002", "ST创业", 10.50, 5.0)]
    s = market_stats(rows)
    assert s["limit_up"] == 0
    rows = [_row("300002", "ST创业", 12.00, 20.0)]
    s = market_stats(rows)
    assert s["limit_up"] == 1


def test_market_stats_limit_down():
    """跌停:10.00 → 9.00(-10%)。"""
    rows = [_row("600519", "贵州茅台", 9.00, -10.0)]
    s = market_stats(rows)
    assert s["limit_down"] == 1 and s["limit_up"] == 0
    assert s["down"] == 1


def test_market_stats_gem_not_10pct():
    """创业板 +10% 未到 20% 板,不算涨停。"""
    rows = [_row("300003", "创业板股", 11.00, 10.0)]
    s = market_stats(rows)
    assert s["limit_up"] == 0
