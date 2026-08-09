import time
from datetime import datetime
from portfolio import Portfolio
from risk_manager import RiskManager

class SimExecutor:
    def __init__(self, initial_cash=100000.0):
        self.portfolio = Portfolio(initial_cash)
        self.risk = RiskManager()
        self.trades = []

    def execute(self, code, name, price, action, market_pct=None):
        today = datetime.now().date().isoformat()
        self.risk.reset_daily(today)

        if action == "buy":
            pct = market_pct if market_pct is not None else 0
            has = code in self.portfolio.positions
            ok, max_amount, reason = self.risk.check_buy(code, price, self.portfolio, pct, has)
            if not ok:
                return {"status": "rejected", "reason": reason}
            qty = self.risk.position_size(price, self.portfolio.total_value({code: price}))
            if qty <= 0:
                return {"status": "rejected", "reason": "计算数量为0"}
            success, msg = self.portfolio.buy(code, name, price, qty)
            if success:
                self.risk.record_trade()
                self.trades.append({"code": code, "action": "buy", "price": price, "qty": qty})
            return {"status": "executed" if success else "failed", "msg": msg}

        elif action == "sell":
            qty = self.portfolio.positions[code]["qty"] if code in self.portfolio.positions else 0
            if qty == 0:
                return {"status": "rejected", "reason": "无持仓"}
            success, msg = self.portfolio.sell(code, name, price, qty)
            if success:
                self.risk.record_trade()
                self.trades.append({"code": code, "action": "sell", "price": price, "qty": qty})
            return {"status": "executed" if success else "failed", "msg": msg}

        return {"status": "rejected", "reason": "未知操作"}

    def get_summary(self, prices):
        mv = self.portfolio.market_value(prices)
        tv = self.portfolio.total_value(prices)
        return {
            "cash": round(self.portfolio.cash, 2),
            "market_value": round(mv, 2),
            "total_value": round(tv, 2),
            "total_return": round(tv - self.portfolio.initial_cash, 2),
            "return_pct": round((tv - self.portfolio.initial_cash) / self.portfolio.initial_cash * 100, 2),
            "positions": len(self.portfolio.positions),
            "trade_count": len(self.trades),
        }

    def is_market_open(self):
        now = datetime.now()
        weekday = now.weekday()
        if weekday >= 5:
            return False, "周末休市"
        hour = now.hour
        minute = now.minute
        if (hour == 9 and minute >= 30) or (10 <= hour <= 11) or (hour == 13 and minute < 0) or (13 <= hour <= 14):
            return True, "交易中"
        return False, "非交易时间"