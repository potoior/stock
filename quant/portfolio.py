import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).parent / "agent_data.db"


def init_db(db_path=None):
    path = Path(db_path) if db_path else DEFAULT_DB
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            code TEXT PRIMARY KEY,
            qty INTEGER,
            cost REAL,
            buy_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            name TEXT,
            action TEXT,
            price REAL,
            qty INTEGER,
            amount REAL,
            pnl REAL,
            date TEXT
        )
    """)
    conn.commit()
    return conn


class Portfolio:
    def __init__(self, initial_cash=100000.0, db_path=None):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions = {}
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self._conn = init_db(self.db_path)
        self._load()

    def _load(self):
        rows = self._conn.execute("SELECT code, qty, cost, buy_date FROM positions").fetchall()
        for code, qty, cost, buy_date in rows:
            self.positions[code] = {"qty": qty, "cost": cost, "buy_date": buy_date}

    def buy(self, code, name, price, qty):
        amount = price * qty
        if amount > self.cash:
            return False, "资金不足"
        if code in self.positions:
            p = self.positions[code]
            total_cost = p["cost"] * p["qty"] + price * qty
            p["qty"] += qty
            p["cost"] = total_cost / p["qty"]
        else:
            self.positions[code] = {"qty": qty, "cost": price, "buy_date": datetime.now().date().isoformat()}
        self.cash -= amount
        self._record_trade(code, name, "买入", price, qty, amount, 0)
        self._save_positions()
        return True, f"买入 {code} {qty} 股 @ {price:.2f}"

    def sell(self, code, name, price, qty):
        if code not in self.positions:
            return False, "无持仓"
        p = self.positions[code]
        if qty > p["qty"]:
            return False, "卖出数量超过持仓"
        pnl = (price - p["cost"]) * qty
        self.cash += price * qty
        p["qty"] -= qty
        if p["qty"] == 0:
            del self.positions[code]
        else:
            self.positions[code] = p
        self._record_trade(code, name, "卖出", price, qty, price * qty, pnl)
        self._save_positions()
        return True, f"卖出 {code} {qty} 股 @ {price:.2f}, 盈亏 {pnl:+.2f}"

    def close_all(self, prices):
        """按当前价格全部平仓"""
        for code in list(self.positions.keys()):
            if code in prices:
                self.sell(code, code, prices[code], self.positions[code]["qty"])

    def _record_trade(self, code, name, action, price, qty, amount, pnl):
        self._conn.execute(
            "INSERT INTO trades (code, name, action, price, qty, amount, pnl, date) VALUES (?,?,?,?,?,?,?,?)",
            (code, name, action, price, qty, amount, pnl, datetime.now().isoformat()),
        )
        self._conn.commit()

    def _save_positions(self):
        # 使用 UPSERT 避免崩溃丢数据；先按当前持仓 upsert，再删除已不在的 code
        existing = {r[0] for r in self._conn.execute("SELECT code FROM positions").fetchall()}
        for code, p in self.positions.items():
            self._conn.execute(
                "INSERT INTO positions (code, qty, cost, buy_date) VALUES (?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET qty=excluded.qty, cost=excluded.cost, buy_date=excluded.buy_date",
                (code, p["qty"], p["cost"], p["buy_date"]),
            )
        stale = existing - set(self.positions.keys())
        for code in stale:
            self._conn.execute("DELETE FROM positions WHERE code=?", (code,))
        self._conn.commit()

    def market_value(self, prices):
        """纯查询：不改持仓对象状态。prices 中缺失的 code 不计入。"""
        total = 0
        for code, p in self.positions.items():
            if code in prices:
                total += prices[code] * p["qty"]
        return total

    def total_value(self, prices):
        return self.cash + self.market_value(prices)

    def reset(self):
        self._conn.execute("DELETE FROM positions")
        self._conn.execute("DELETE FROM trades")
        self._conn.commit()
        self.positions = {}
        self.cash = self.initial_cash

    def get_positions(self):
        return self.positions

    def get_trades(self, limit=100):
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return rows

    def close(self):
        self._conn.close()
