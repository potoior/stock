---
description: 每日开盘扫描：爬全A股≈5500只+新闻，生成AI综合日报。建议每个交易日09:00后运行。
agent: build
---

运行每日开盘扫描并生成综合日报。请在 quant 目录下执行：

```bash
cd /media/lpy/sda3/stock/quant && python daily_scan.py
```

执行完成后：

1. 读取生成的日报文件 `quant/reports/daily_YYYYMMDD.md`（用今天日期）。
2. 把日报的「一、全市场扫描」「二、成交额 top 候选与策略信号」「四、AI 综合分析」三部分内容完整展示给我。
3. 如果 top 候选里有「买入」信号且新闻面有对应利好，重点提示。
4. 若脚本因网络或 AI 限流失败，告诉我具体错误并建议重试。

可选参数（用户可在命令后追加）：
- `$ARGUMENTS` 为空时默认全市场扫描；若用户写了 `--limit 1000` 等参数，拼到命令末尾再执行。
