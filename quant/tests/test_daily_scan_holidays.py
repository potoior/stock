"""daily_scan 节假日判断测试。"""

from datetime import datetime

import daily_scan


def test_is_trading_day_weekday():
    """周一非节假日应为交易日。"""
    # 2026-08-24 是周一
    monday = datetime(2026, 8, 24, 9, 0, 0)
    assert daily_scan.is_trading_day(monday) is True


def test_is_trading_day_weekend():
    """周六应为非交易日。"""
    sat = datetime(2026, 8, 22, 9, 0, 0)  # 周六
    assert daily_scan.is_trading_day(sat) is False
    sun = datetime(2026, 8, 23, 9, 0, 0)  # 周日
    assert daily_scan.is_trading_day(sun) is False


def test_is_trading_day_holiday():
    """春节假期应为非交易日(即使在工作日)。"""
    # 2026-02-18 是周三,但在春节假期内
    chunjie = datetime(2026, 2, 18, 9, 0, 0)
    assert daily_scan.is_trading_day(chunjie) is False


def test_is_trading_day_national_day():
    """国庆 10-01 应为非交易日。"""
    national = datetime(2026, 10, 1, 9, 0, 0)
    assert daily_scan.is_trading_day(national) is False


def test_is_trading_day_after_holiday():
    """国庆后 10-08(周四)应为交易日。"""
    after = datetime(2026, 10, 8, 9, 0, 0)
    assert daily_scan.is_trading_day(after) is True


def test_is_trading_day_default_now():
    """不传参数用当前时间(不应抛异常)。"""
    result = daily_scan.is_trading_day()
    assert isinstance(result, bool)
