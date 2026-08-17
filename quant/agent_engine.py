import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

ENGINE_HOME = Path(__file__).parent
AI_DB = ENGINE_HOME / "agent_data_ai.db"
RULE_DB = ENGINE_HOME / "agent_data_rule.db"
LOG_DB = ENGINE_HOME / "agent_data.db"

import strategy_engine as se
from executor import SimExecutor, is_market_open


def _init_log_db():
    conn = sqlite3.connect(str(LOG_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_type TEXT, ts TEXT, value REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent_type TEXT, code TEXT, name TEXT,
            action TEXT, price REAL, qty INTEGER, reason TEXT
        )
    """)
    conn.commit()
    conn.close()


_init_log_db()


def _save_equity_point(agent_type, ts, value):
    conn = sqlite3.connect(str(LOG_DB))
    conn.execute("INSERT INTO equity_curve (agent_type, ts, value) VALUES (?,?,?)", (agent_type, ts, value))
    conn.commit()
    conn.close()


def _save_log(record):
    """record: dict with keys agent_type, ts, code, name, action, price, qty, reason"""
    conn = sqlite3.connect(str(LOG_DB))
    conn.execute(
        "INSERT INTO agent_logs (ts, agent_type, code, name, action, price, qty, reason) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            record["ts"],
            record["agent_type"],
            record["code"],
            record["name"],
            record["action"],
            record["price"],
            record["qty"],
            record["reason"],
        ),
    )
    conn.commit()
    conn.close()


def _load_equity_curve(agent_type, limit=500):
    conn = sqlite3.connect(str(LOG_DB))
    rows = conn.execute(
        "SELECT ts, value FROM equity_curve WHERE agent_type=? ORDER BY id DESC LIMIT ?", (agent_type, limit)
    ).fetchall()
    conn.close()
    rows = list(reversed(rows))
    return [{"t": r[0], "v": r[1]} for r in rows]


def _load_logs(limit=100):
    conn = sqlite3.connect(str(LOG_DB))
    rows = conn.execute(
        "SELECT ts, agent_type, code, name, action, price, qty, reason "
        "FROM agent_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "ts": r[0],
            "agent_type": r[1],
            "code": r[2],
            "name": r[3],
            "action": r[4],
            "price": r[5],
            "qty": r[6],
            "reason": r[7],
        }
        for r in rows
    ]


def _clear_logs():
    conn = sqlite3.connect(str(LOG_DB))
    conn.execute("DELETE FROM equity_curve")
    conn.execute("DELETE FROM agent_logs")
    conn.commit()
    conn.close()


class AgentEngine:
    def __init__(self, interval=60):
        self.interval = interval
        self.running = False
        self.thread = None
        self.last_run = None
        self.cycle_count = 0
        self.ai_decider = None
        self.last_cycle_summary = ""

        self.ai_executor = SimExecutor(initial_cash=10000.0, db_path=AI_DB)
        self.rule_executor = SimExecutor(initial_cash=10000.0, db_path=RULE_DB)

        self.ai_history = _load_equity_curve("ai")
        self.rule_history = _load_equity_curve("rule")
        self._init_ai_decider()

    def _init_ai_decider(self):
        try:
            from ai_decider import AIDecider

            self.ai_decider = AIDecider()
        except Exception:
            self.ai_decider = None

    def start(self):
        if self.running:
            return {"ok": False, "msg": "已在运行中"}
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return {"ok": True, "msg": "已启动"}

    def stop(self):
        if not self.running:
            return {"ok": False, "msg": "未在运行"}
        self.running = False
        self.thread = None
        return {"ok": True, "msg": "已停止"}

    def reset(self):
        self.running = False
        self.ai_executor = SimExecutor(initial_cash=10000.0, db_path=AI_DB)
        self.rule_executor = SimExecutor(initial_cash=10000.0, db_path=RULE_DB)
        self.ai_history = []
        self.rule_history = []
        self.cycle_count = 0
        self.last_run = None
        self.last_cycle_summary = ""
        _clear_logs()
        return {"ok": True, "msg": "已重置"}

    def status(self):
        ai_prices = self._get_current_prices(self.ai_executor.portfolio.positions)
        rule_prices = self._get_current_prices(self.rule_executor.portfolio.positions)
        ai_sum = self.ai_executor.get_summary(ai_prices)
        rule_sum = self.rule_executor.get_summary(rule_prices)

        def fmt_positions(executor, prices):
            rows = []
            for code, p in executor.portfolio.positions.items():
                price = prices.get(code, 0)
                pnl = (price - p["cost"]) * p["qty"]
                pnl_pct = (price - p["cost"]) / p["cost"] * 100 if p["cost"] else 0
                rows.append(
                    {
                        "code": code,
                        "qty": p["qty"],
                        "cost": round(p["cost"], 2),
                        "price": round(price, 2),
                        "pnl": round(pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "buy_date": p.get("buy_date", ""),
                    }
                )
            return rows

        return {
            "running": self.running,
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "cycle_count": self.cycle_count,
            "last_cycle_summary": self.last_cycle_summary,
            "ai": {
                "cash": ai_sum["cash"],
                "market_value": ai_sum["market_value"],
                "total_value": ai_sum["total_value"],
                "total_return": ai_sum["total_return"],
                "return_pct": ai_sum["return_pct"],
                "positions": fmt_positions(self.ai_executor, ai_prices),
                "trade_count": ai_sum["trade_count"],
            },
            "rule": {
                "cash": rule_sum["cash"],
                "market_value": rule_sum["market_value"],
                "total_value": rule_sum["total_value"],
                "total_return": rule_sum["total_return"],
                "return_pct": rule_sum["return_pct"],
                "positions": fmt_positions(self.rule_executor, rule_prices),
                "trade_count": rule_sum["trade_count"],
            },
            "ai_history": self.ai_history[-200:],
            "rule_history": self.rule_history[-200:],
        }

    def trades(self, type_filter=None, limit=50):
        executors = []
        if type_filter in (None, "ai"):
            executors.append(("ai", self.ai_executor))
        if type_filter in (None, "rule"):
            executors.append(("rule", self.rule_executor))
        all_trades = []
        for atype, ex in executors:
            for t in ex.portfolio.get_trades(limit=limit):
                all_trades.append(
                    {
                        "id": t[0],
                        "code": t[1],
                        "name": t[2],
                        "action": t[3],
                        "price": t[4],
                        "qty": t[5],
                        "amount": t[6],
                        "pnl": t[7],
                        "date": t[8],
                        "agent_type": atype,
                    }
                )
        all_trades.sort(key=lambda x: x["date"], reverse=True)
        return all_trades[:limit]

    def logs(self, limit=100):
        return _load_logs(limit)

    def _loop(self):
        while self.running:
            if self._is_market_hours():
                try:
                    self._cycle()
                except Exception:
                    import traceback

                    traceback.print_exc()
                self.last_run = datetime.now()
                self.cycle_count += 1
            time.sleep(self.interval)

    def _is_market_hours(self):
        open_, _ = is_market_open()
        return open_

    def _get_current_prices(self, positions):
        codes = list(positions.keys())
        if not codes:
            return {}
        quotes = se.fetch_realtime(codes)
        return {q["code"]: q["price"] for q in quotes}

    def _cycle(self):
        if not self._is_market_hours():
            return

        watchlist = se.get_watchlist()
        if not watchlist:
            return
        codes = [w["code"] for w in watchlist]
        quotes = se.fetch_realtime(codes)
        prices = {q["code"]: q["price"] for q in quotes}

        analysis_results = {}
        for q in quotes:
            result = se.analyze(q["code"], use_ai=False)
            if "error" not in result:
                analysis_results[q["code"]] = result

        rule_actions = self._rule_cycle(quotes, prices, analysis_results)
        ai_actions = self._ai_cycle(quotes, prices, analysis_results)

        ai_prices = self._get_current_prices(self.ai_executor.portfolio.positions)
        rule_prices = self._get_current_prices(self.rule_executor.portfolio.positions)
        ai_tv = self.ai_executor.get_summary(ai_prices)["total_value"]
        rule_tv = self.rule_executor.get_summary(rule_prices)["total_value"]
        ts = datetime.now().strftime("%H:%M")
        self.ai_history.append({"t": ts, "v": round(ai_tv, 2)})
        self.rule_history.append({"t": ts, "v": round(rule_tv, 2)})
        _save_equity_point("ai", ts, round(ai_tv, 2))
        _save_equity_point("rule", ts, round(rule_tv, 2))

        summary_parts = []
        if rule_actions:
            summary_parts.append("规则: " + "; ".join(rule_actions))
        if ai_actions:
            summary_parts.append("AI: " + "; ".join(ai_actions))
        self.last_cycle_summary = " | ".join(summary_parts) if summary_parts else "本轮无交易"

    def _rule_cycle(self, quotes, prices, analysis_results):
        ex = self.rule_executor
        today = datetime.now().date().isoformat()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        daily_trades = 0
        actions = []
        for q in quotes:
            code = q["code"]
            if daily_trades >= 5:
                break
            result = analysis_results.get(code)
            if not result:
                continue
            pos = ex.portfolio.positions.get(code)
            price = q["price"]

            if pos:
                pnl_pct = (price - pos["cost"]) / pos["cost"]
                if pnl_pct <= -0.05:
                    ex.execute(code, q["name"], price, "sell")
                    daily_trades += 1
                    reason = f"止损卖出({pnl_pct * 100:.1f}%)"
                    _save_log(
                        {
                            "agent_type": "rule",
                            "ts": ts,
                            "code": code,
                            "name": q["name"],
                            "action": "sell",
                            "price": price,
                            "qty": pos["qty"],
                            "reason": reason,
                        }
                    )
                    actions.append(f"卖出{code}止损({pnl_pct * 100:.1f}%)")
                    continue

            can_sell = not (pos and pos.get("buy_date") == today)
            verdict = result["verdict"]
            if verdict == "买入" and not pos:
                total_value = ex.portfolio.total_value({code: price})
                max_amount = total_value * 0.2
                qty = int(max_amount / price / 100) * 100
                if qty >= 100:
                    ex.execute(code, q["name"], price, "buy")
                    daily_trades += 1
                    reason = f"策略投票买入(买入{result['summary']['buy']}票)"
                    _save_log(
                        {
                            "agent_type": "rule",
                            "ts": ts,
                            "code": code,
                            "name": q["name"],
                            "action": "buy",
                            "price": price,
                            "qty": qty,
                            "reason": reason,
                        }
                    )
                    actions.append(f"买入{code} {qty}股")
            elif verdict == "卖出" and pos and can_sell:
                ex.execute(code, q["name"], price, "sell")
                daily_trades += 1
                reason = f"策略投票卖出(卖出{result['summary']['sell']}票)"
                _save_log(
                    {
                        "agent_type": "rule",
                        "ts": ts,
                        "code": code,
                        "name": q["name"],
                        "action": "sell",
                        "price": price,
                        "qty": pos["qty"],
                        "reason": reason,
                    }
                )
                actions.append(f"卖出{code} {pos['qty']}股")
        return actions

    def _ai_cycle(self, quotes, prices, analysis_results):
        if not self.ai_decider:
            return []
        ex = self.ai_executor
        today = datetime.now().date().isoformat()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        actions = []

        for q in quotes:
            code = q["code"]
            pos = ex.portfolio.positions.get(code)
            if pos:
                pnl_pct = (q["price"] - pos["cost"]) / pos["cost"]
                if pnl_pct <= -0.05:
                    ex.execute(code, q["name"], q["price"], "sell")
                    reason = f"硬性止损({pnl_pct * 100:.1f}%)"
                    _save_log(
                        {
                            "agent_type": "ai",
                            "ts": ts,
                            "code": code,
                            "name": q["name"],
                            "action": "sell",
                            "price": q["price"],
                            "qty": pos["qty"],
                            "reason": reason,
                        }
                    )
                    actions.append(f"卖出{code}止损({pnl_pct * 100:.1f}%)")

        market_data = []
        for q in quotes:
            code = q["code"]
            result = analysis_results.get(code)
            if not result:
                continue
            ind = result.get("indicators", {})
            sigs = result.get("signals", [])
            buy_n = sum(1 for s in sigs if s["signal"] == "buy")
            sell_n = sum(1 for s in sigs if s["signal"] == "sell")
            hold_n = sum(1 for s in sigs if s["signal"] == "hold")
            info = {
                "code": code,
                "name": q["name"],
                "price": q["price"],
                "pct": q.get("pct", 0),
                "indicators": {
                    "macd": f"{ind.get('macd_diff', 0)}/{ind.get('macd_dea', 0)}",
                    "kdj": f"{ind.get('k', 0)}/{ind.get('d', 0)}/{ind.get('j', 0)}",
                    "boll": f"{ind.get('boll_u', 0)}/{ind.get('boll_m', 0)}/{ind.get('boll_l', 0)}",
                    "ma": f"{ind.get('ma5', 0)}/{ind.get('ma10', 0)}/{ind.get('ma60', 0)}",
                    "psy": ind.get("psy", 0),
                    "bias": f"{ind.get('bias1', 0)}/{ind.get('bias2', 0)}/{ind.get('bias3', 0)}",
                    "dmi": f"{ind.get('pdi', 0)}/{ind.get('mdi', 0)}/{ind.get('adx', 0)}",
                    "sar": ind.get("sar", 0),
                    "tower": ind.get("tower", 0),
                },
                "verdict": result.get("verdict", "观望"),
                "votes": f"买入{buy_n}票/卖出{sell_n}票/观望{hold_n}票",
                "total": len(sigs),
            }
            market_data.append(info)

        if not market_data:
            return []

        prompt = self._build_agent_prompt(market_data, ex, prices)
        raw = self.ai_decider._call_api(prompt, timeout=45)
        decisions = self._parse_agent_response(raw, market_data)

        daily_trades = 0
        for dec in decisions:
            if daily_trades >= 5:
                break
            code = dec["code"]
            action = dec["action"]
            qty = dec.get("qty", 0)
            reason = dec.get("reason", "")
            q = next((x for x in quotes if x["code"] == code), None)
            if not q:
                continue
            pos = ex.portfolio.positions.get(code)
            price = q["price"]

            if action == "buy" and not pos:
                qty = int(qty / 100) * 100
                max_qty = int(ex.portfolio.total_value({code: price}) * 0.2 / price / 100) * 100
                qty = min(qty, max_qty) if qty > 0 else max_qty
                if qty >= 100:
                    ex.execute(code, q["name"], price, "buy")
                    daily_trades += 1
                    _save_log(
                        {
                            "agent_type": "ai",
                            "ts": ts,
                            "code": code,
                            "name": q["name"],
                            "action": "buy",
                            "price": price,
                            "qty": qty,
                            "reason": reason,
                        }
                    )
                    actions.append(f"买入{code} {qty}股({reason})")
            elif action == "sell" and pos:
                if pos.get("buy_date") == today:
                    _save_log(
                        {
                            "agent_type": "ai",
                            "ts": ts,
                            "code": code,
                            "name": q["name"],
                            "action": "hold",
                            "price": price,
                            "qty": 0,
                            "reason": f"T+1限制: {reason}",
                        }
                    )
                    continue
                ex.execute(code, q["name"], price, "sell")
                daily_trades += 1
                _save_log(
                    {
                        "agent_type": "ai",
                        "ts": ts,
                        "code": code,
                        "name": q["name"],
                        "action": "sell",
                        "price": price,
                        "qty": pos["qty"],
                        "reason": reason,
                    }
                )
                actions.append(f"卖出{code} {pos['qty']}股({reason})")
        return actions

    def _build_agent_prompt(self, market_data, executor, prices):
        total_value = executor.portfolio.total_value(prices)
        position_lines = []
        for code, p in executor.portfolio.positions.items():
            price = prices.get(code, 0)
            pnl_pct = (price - p["cost"]) / p["cost"] * 100 if p["cost"] else 0
            position_lines.append(
                f"- {code}: {p['qty']}股, 成本{p['cost']:.2f}, 现价{price:.2f}, 盈亏{pnl_pct:+.1f}%, 买入日{p.get('buy_date', '')}"
            )
        stock_lines = []
        for s in market_data:
            stock_lines.append(
                f"- {s['name']}({s['code']}): 现价{s['price']:.2f} 涨跌{s['pct']:+.2f}% "
                f"MACD={s['indicators']['macd']} KDJ={s['indicators']['kdj']} "
                f"BOLL={s['indicators']['boll']} 均线={s['indicators']['ma']} "
                f"PSY={s['indicators']['psy']} BIAS={s['indicators']['bias']} "
                f"DMI={s['indicators']['dmi']} SAR={s['indicators']['sar']} "
                f"宝塔={s['indicators']['tower']} "
                f"投票={s['votes']} 结论={s['verdict']}"
            )
        return f"""你是一个A股短线交易AI。根据以下数据独立判断每只股票的操作建议。

## 可用资金
现金: {executor.portfolio.cash:.2f} 元
总资产: {total_value:.2f} 元

## 当前持仓
{chr(10).join(position_lines) if position_lines else "空仓"}

## 市场行情与技术指标
{chr(10).join(stock_lines)}

## 交易规则
- A股T+1：今日买入的股票今日不能卖出
- 每笔最少100股，必须为100的整数倍
- 单只股票持仓不超过总资产的20%
- 已有持仓的股票不要重复买入
- 每日最多交易5次
- 交易时间9:30-11:30, 13:00-15:00

## 输出格式（严格JSON数组，不要markdown）
[
  {{"code": "600789", "action": "buy/sell/hold", "qty": 100, "reason": "理由"}},
  ...
]"""

    def _parse_agent_response(self, text, market_data):
        import re

        expected = {s["code"] for s in market_data}
        try:
            m = re.search(r"\[.*\]", text, re.DOTALL)
            if not m:
                return [{"code": c, "action": "hold", "qty": 0, "reason": "AI解析失败"} for c in expected]
            data = json.loads(m.group())
            if not isinstance(data, list):
                raise ValueError
            out = []
            for item in data:
                code = str(item.get("code", ""))
                action = str(item.get("action", "hold"))
                qty = int(item.get("qty", 0))
                reason = str(item.get("reason", ""))
                if action not in ("buy", "sell", "hold"):
                    action = "hold"
                if code in expected:
                    out.append({"code": code, "action": action, "qty": qty, "reason": reason})
                    expected.discard(code)
            for c in expected:
                out.append({"code": c, "action": "hold", "qty": 0, "reason": "AI未返回此代码"})
            return out
        except Exception:
            return [{"code": c, "action": "hold", "qty": 0, "reason": "AI解析失败"} for c in expected]


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = AgentEngine()
    return _engine
