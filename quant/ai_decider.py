import json
import re
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "opencode" / "opencode.jsonc"

def load_api_key():
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = [l for l in text.split("\n") if not l.lstrip().startswith("//")]
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
                f"涨跌{s['pct']:+.2f}%, MA5={s.get('ma5','-')}, "
                f"MACD={'多头' if s.get('macd_bull') else '空头'}, "
                f"KDJ={s.get('kdj_signal','-')}"
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

    def _call_api(self, prompt):
        import httpx
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 65536,
        }
        try:
            with httpx.Client(timeout=60, verify=False) as client:
                resp = client.post(
                    "https://token.sensenova.cn/v1/chat/completions",
                    json=payload, headers=headers
                )
                if resp.status_code == 200:
                    msg = resp.json()["choices"][0]["message"]
                    content = msg.get("content") or ""
                    reasoning = msg.get("reasoning") or ""
                    return content + "\n" + reasoning
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

if __name__ == "__main__":
    from executor import SimExecutor
    ex = SimExecutor()
    d = AIDecider()
    result = d.decide({"quotes": []}, ex.portfolio)
    print(json.dumps(result, ensure_ascii=False, indent=2))