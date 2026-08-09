class RiskManager:
    """风险控制模块

    不可逾越的红线：
    1. 永不全仓单只股票
    2. 永不加仓摊平亏损
    3. 单笔亏损超过 5% 必须止损
    4. 大盘跌超 3% 当日停止交易
    5. 单日交易不超过 5 次
    """
    def __init__(self, config=None):
        default = {
            "max_single_position": 0.20,   # 单股占总资金上限 20%
            "max_total_position": 0.80,    # 总仓位上限 80%
            "stop_loss_pct": 0.05,         # 单笔止损 5%
            "take_profit_pct": 0.20,       # 止盈 20%
            "max_daily_trades": 5,         # 单日最大交易次数
            "market_crash_pct": 0.03,      # 大盘跌超 3% 停止交易
            "no_averaging_down": True,     # 禁止摊平亏损
        }
        if config:
            default.update(config)
        self.config = default
        self.daily_trades = 0
        self.today = None

    def reset_daily(self, today):
        """新的一天重置交易计数"""
        if self.today != today:
            self.today = today
            self.daily_trades = 0

    def check_buy(self, code, price, portfolio, market_pct, position_has):
        """检查是否可以买入，返回 (是否允许, 建议买入资金, 原因)"""
        if market_pct is not None and market_pct < -self.config["market_crash_pct"]:
            return False, 0, f"大盘跌 {market_pct:.2f}%，今日停止买入"
        if self.daily_trades >= self.config["max_daily_trades"]:
            return False, 0, "今日交易次数已达上限"
        if position_has and self.config["no_averaging_down"]:
            return False, 0, "已有持仓，禁止摊平"
        total_value = portfolio.total_value({code: price})
        per_position = total_value * self.config["max_single_position"]
        if portfolio.cash < per_position:
            return False, 0, "可用资金不足"
        return True, per_position, "允许买入"

    def check_sell(self, code, price, portfolio):
        """检查是否需要止损/止盈，返回 (是否卖出, 原因)"""
        if code not in portfolio.positions:
            return False, "无持仓"
        p = portfolio.positions[code]
        pnl_pct = (price - p["cost"]) / p["cost"] if p["cost"] else 0
        if pnl_pct <= -self.config["stop_loss_pct"]:
            return True, f"止损触发，亏损 {pnl_pct*100:.2f}%"
        if pnl_pct >= self.config["take_profit_pct"]:
            return True, f"止盈触发，盈利 {pnl_pct*100:.2f}%"
        return False, ""

    def record_trade(self):
        self.daily_trades += 1

    def position_size(self, price, total_value):
        """计算买入数量（按单股 20% 上限）"""
        max_amount = total_value * self.config["max_single_position"]
        qty = int(max_amount / price)
        return max(qty, 0)

    def get_status(self):
        return dict(self.config)