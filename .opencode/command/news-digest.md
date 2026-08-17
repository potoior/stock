---
description: 每日财经新闻AI分析日报：抓新浪+东财双源新闻，生成市场情绪/板块/风险日报。随时可运行。
agent: build
---

运行每日新闻分析并生成日报。请在 quant 目录下执行：

```bash
cd /media/lpy/sda3/stock/quant && python news_digest.py
```

执行完成后：

1. 读取生成的日报文件 `quant/reports/news_YYYYMMDD.md`（用今天日期）。
2. 把「AI 分析」部分（市场情绪总览 / 关注板块 / 关键事件解读 / 风险提示 / 操作参考）完整展示给我。
3. 若脚本因网络或 AI 限流失败，告诉我具体错误并建议重试。

可选参数：`$ARGUMENTS`（如 `--limit 10`、`--no-ai`）拼到命令末尾再执行。
