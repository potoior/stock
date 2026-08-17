import json
import os
import re
import time
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.jsonc"

_last_call_ts = 0.0
_MIN_INTERVAL = 0.8  # 两次AI调用最小间隔，降频避免触发限流


def load_api_key():
    # 优先环境变量，避免与 opencode 配置文件强耦合
    env_key = os.environ.get("SENSENOVA_API_KEY") or os.environ.get("AI_API_KEY")
    if env_key:
        return env_key
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = [line for line in text.split("\n") if not line.lstrip().startswith("//")]
    text = re.sub(r",\s*}", "}", re.sub(r",\s*]", "]", "\n".join(lines)))
    config = json.loads(text)
    return config["provider"]["sensenova"]["options"]["apiKey"]


class AIDecider:
    def __init__(self, model="sensenova-6.7-flash-lite"):
        self.api_key = load_api_key()
        self.model = model

    def decide(self, market_data, portfolio):
        """AI 综合决策：分析市场并给出交易建议"""
        prompt = self._build_prompt(market_data, portfolio)
        response = self._call_api(prompt)
        return self._parse_response(response)

    def _build_prompt(self, data, portfolio):
        stocks = data.get("quotes", [])
        lines = []
        for s in stocks:
            lines.append(
                f"- {s['name']}({s['code']}): 现价{s['price']:.2f}, "
                f"涨跌{s['pct']:+.2f}%, MA5={s.get('ma5', '-')}, "
                f"MACD={'多头' if s.get('macd_bull') else '空头'}, "
                f"KDJ={s.get('kdj_signal', '-')}"
            )
        pos_lines = []
        for code, p in portfolio.positions.items():
            pos_lines.append(f"- {code}: {p['qty']}股, 成本{p['cost']:.2f}")

        return f"""你是一个A股短线交易AI，请基于以下数据做出交易决策，输出JSON格式。

## 当前持仓
{chr(10).join(pos_lines) if pos_lines else "空仓"}
总资产: {portfolio.total_value({}):.2f}
可用现金: {portfolio.cash:.2f}

## 市场行情
{chr(10).join(lines)}

## 交易规则
- 单笔买入不超过总资产 20%
- 已有持仓的股票不要重复买入
- 亏损超过 5% 必须卖出止损
- 盈利超过 20% 考虑止盈
- 每天最多交易 5 次

## 输出格式（严格JSON，不要其他文字）
{{
  "actions": [
    {{"code": "600789", "action": "buy/sell/hold", "reason": "分析理由"}}
  ],
  "market_judgment": "看多/看空/震荡",
  "risk_level": "低/中/高"
}}"""

    def _call_api(self, prompt, timeout=60):
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 4096,
        }
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(
                    "https://token.sensenova.cn/v1/chat/completions", json=payload, headers=headers
                )
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning") or ""
                    return content + "\n" + reasoning
                if resp.status_code in (429, 503, 500):
                    return f"API限流: {resp.status_code}"
                return f"API错误: {resp.status_code}"
        except Exception as e:
            return f"调用失败: {e}"

    def _parse_response(self, text):
        try:
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return {"actions": [], "market_judgment": "未知", "risk_level": "中"}
        except json.JSONDecodeError:
            return {"actions": [], "market_judgment": "解析失败", "risk_level": "中"}

    def judge_code(self, code, name, indicators_text, rules, timeout=45, max_retries=2):
        """根据自然语言规则 + 指标值，让 AI 判断每只股票的买卖。

        indicators_text: 指标值文本
        rules: [{id, name, buy_rule, sell_rule}]
        返回: {code, results: [{id, signal: buy/sell/hold, reason}]}
        """
        if not rules:
            return {"code": code, "results": []}
        prompt = self._build_judge_prompt(code, name, indicators_text, rules)
        raw = None
        for attempt in range(max_retries + 1):
            global _last_call_ts
            now = time.time()
            if now - _last_call_ts < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - (now - _last_call_ts))
            _last_call_ts = time.time()
            raw = self._call_api(prompt, timeout=timeout)
            if raw.startswith("API限流"):
                # 限流/服务不可用 → 指数退避后重试
                if attempt < max_retries:
                    time.sleep(2 * (attempt + 1))
                continue
            if isinstance(raw, str) and not raw.startswith(("API错误", "调用失败", "API限流")):
                break
            if attempt < max_retries:
                time.sleep(1.5)
        if not isinstance(raw, str) or raw.startswith(("API错误", "调用失败", "API限流")):
            fallback = [{"id": r["id"], "signal": "hold", "reason": f"AI判定不可用: {raw}"} for r in rules]
            return {"code": code, "results": fallback}
        parsed = self._parse_judge_response(raw, [r["id"] for r in rules])
        return {"code": code, "results": parsed}

    def _build_judge_prompt(self, code, name, indicators_text, rules):
        rule_lines = []
        for r in rules:
            rule_lines.append(
                f"- 规则[{r['id']}] {r['name']}：买入条件「{r.get('buy_rule', '')}」；"
                f"卖出条件「{r.get('sell_rule', '')}」"
            )
        return f"""你是一位资深A股短线技术分析师。请根据给定的技术指标，严格套用下面的用户规则，判断每个规则当前应该发出什么信号。

## 股票
{name}({code})

## 当前技术指标值
{indicators_text}

## 用户规则
{chr(10).join(rule_lines)}

## 判定要求
- 对每条规则独立判断，输出 buy(买入) / sell(卖出) / hold(观望) 三选一
- 先看卖出条件是否满足，满足则 sell；否则看买入条件是否满足，满足则 buy；都不满足则 hold
- 理由要具体引用指标数值，30字以内
- 只输出JSON数组（不要markdown），格式:
{{"results": [{{"id": "规则id", "signal": "buy/sell/hold", "reason": "理由"}}]}}"""

    def _parse_judge_response(self, text, expected_ids):
        try:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if not m:
                return [{"id": i, "signal": "hold", "reason": "AI解析失败"} for i in expected_ids]
            data = json.loads(m.group())
            results = data.get("results", [])
            out = []
            for rid in expected_ids:
                hit = next((x for x in results if str(x.get("id")) == str(rid)), None)
                if hit and hit.get("signal") in ("buy", "sell", "hold"):
                    out.append({"id": rid, "signal": hit["signal"], "reason": hit.get("reason", "")})
                else:
                    out.append({"id": rid, "signal": "hold", "reason": "AI未返回该规则结果"})
            return out
        except Exception:
            return [{"id": i, "signal": "hold", "reason": "AI解析失败"} for i in expected_ids]


if __name__ == "__main__":
    from executor import SimExecutor

    ex = SimExecutor()
    d = AIDecider()
    result = d.decide({"quotes": []}, ex.portfolio)
    print(json.dumps(result, ensure_ascii=False, indent=2))
