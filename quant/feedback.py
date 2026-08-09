import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "agent_data.db"

class Feedback:
    """自我反馈模块：分析交易表现，检测策略失效，给出优化建议"""
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))

    def recent_trades(self, limit=100):
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return rows

    def win_rate(self, limit=20):
        """近 limit 笔已平仓交易的胜率"""
        trades = self.recent_trades(limit)
        if not trades:
            return 0, 0
        wins = sum(1 for t in trades if t[7] and t[7] > 0)  # pnl列
        return wins / len(trades), len(trades)

    def profit_loss_ratio(self, limit=100):
        """盈亏比 = 平均盈利 / 平均亏损"""
        trades = self.recent_trades(limit)
        profits = [t[7] for t in trades if t[7] and t[7] > 0]
        losses = [t[7] for t in trades if t[7] and t[7] < 0]
        if not profits or not losses:
            return 0
        avg_profit = sum(profits) / len(profits)
        avg_loss = abs(sum(losses) / len(losses))
        return avg_profit / avg_loss if avg_loss > 0 else 0

    def analyze(self, alert_threshold=None):
        """综合分析，返回诊断报告"""
        if alert_threshold is None:
            alert_threshold = {"win_rate": 0.3, "pl_ratio": 1.0}
        win_rate, total = self.win_rate(20)
        pl_ratio = self.profit_loss_ratio(100)
        trades = self.recent_trades(10)

        report = {
            "timestamp": datetime.now().isoformat(),
            "recent_win_rate": round(win_rate, 3),
            "recent_trades": total,
            "profit_loss_ratio": round(pl_ratio, 3),
            "last_trades": [self._format_trade(t) for t in trades],
            "alerts": [],
            "suggestions": [],
        }

        if total >= 5 and win_rate < alert_threshold["win_rate"]:
            report["alerts"].append(f"近期胜率 {win_rate:.1%} 过低，策略可能失效")
        if pl_ratio < alert_threshold["pl_ratio"]:
            report["alerts"].append(f"盈亏比 {pl_ratio:.2f} 偏低，止损可能过宽")
        if total < 5:
            report["suggestions"].append("交易样本不足，建议积累更多交易数据")
        if win_rate >= 0.5 and pl_ratio >= 1.5:
            report["suggestions"].append("策略表现良好，可考虑增加仓位")
        if any(t[7] and t[7] < 0 for t in trades[:5]):
            report["suggestions"].append("近期连续亏损，建议暂停交易观察")

        return report

    def _format_trade(self, t):
        return {
            "id": t[0], "code": t[1], "name": t[2], "action": t[3],
            "price": t[4], "qty": t[5], "amount": t[6], "pnl": t[7],
            "date": t[8],
        }

    def close(self):
        self.conn.close()