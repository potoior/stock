# AGENTS.md - A 股量化系统项目约定

> 给协作 Agent / 未来自己 / 新接手者看的快速上手文档。

## 项目概览

A 股量化分析系统,集成飞书群聊 Bot(Function Calling ReAct Agent),覆盖:
- 数据获取(腾讯/新浪接口,日 K 线 + 实时价)
- 23 内置策略信号引擎(MACD/KDJ/BOLL/RSI/玉姐 10 条规则等)
- 玉姐精选全市场扫描(多因子评分排行)
- 回测 + 参数网格寻优
- 飞书 Bot Agent(15 个 skill,跨轮记忆,自选股)
- 策略大全(4 来源 73 策略:漫画书 29 + 操练大全 32 + 玉姐 10 + AI 2)

## 目录结构

```
quant/
├── feishu_bot.py          # 飞书长连接 Agent(~2200 行,15 skill)
├── feishu_image.py        # matplotlib 图表(K 线/玉姐/回测/市场)
├── feishu.py              # 飞书 webhook 推送(日报/告警)
├── stock_names.py         # 股票名称解析(腾讯 smartbox + sqlite 缓存)
├── strategy_engine.py     # 23 策略信号引擎
├── backtest_builtin.py    # 回测引擎(workers=1,非线程安全)
├── yujie_scan.py          # 玉姐精选评分(10 条规则)
├── daily_scan.py          # 每日 09:00 全市场扫描 + 飞书日报
├── strategy_library.json  # 策略大全(4 来源 73 策略)
├── data_fetcher.py        # 数据获取(腾讯/新浪)
├── api.py                 # FastAPI 服务(/api/* 端点)
├── dashboard.py           # Gradio 看板
├── ai_decider.py          # AI 决策器(本地网关 LLM)
├── config.json            # 配置(.gitignore,含飞书 app_id/secret)
├── .env                   # 环境变量(.gitignore,AI_API_KEY 等)
├── tests/                 # pytest 测试
│   ├── test_feishu_bot.py     # 25+18 用例(历史/分段/重置/自选股/校验/锁)
│   ├── test_feishu.py        # 16 用例(飞书推送)
│   └── ...
└── reports/               # 日报输出(daily_YYYYMMDD.md)
```

## 运行环境

- **OS**: Ubuntu 24.04,Python 3.12.3(本地) / 3.11(CI)
- **venv**: `quant/.venv/bin/python`
- **部署**: systemd user service,不用 Docker
- **API 端口**: 18000(`PORT` 环境变量,`HOST=0.0.0.0`)
- **AI 网关**: `http://10.10.250.219/v1/chat/completions`,模型 `DSV4-Flash-0731-1M`
- **飞书长连接**: `wss://msg-frontier.feishu.cn/ws/v2`,lark-oapi WsClient,auto_reconnect=True

## 常用命令

```bash
# 跑测试(必须加 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 绕过 ROS launch_testing 冲突)
cd quant && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest

# Lint
.venv/bin/ruff check .

# 启动 API 服务
systemctl --user start quant-api.service

# 启动/重启飞书 Bot
systemctl --user restart feishu-bot.service
systemctl --user status feishu-bot.service
journalctl --user -u feishu-bot -f

# CLI 测试 Agent(不走飞书)
.venv/bin/python feishu_bot.py --agent "分析茅台"

# 单次跑日报
.venv/bin/python daily_scan.py
```

## 关键约定

### 部署
- **systemd user service**,配置在 `~/.config/systemd/user/feishu-bot.service` 和 `quant-api.service`
- 都 `enabled`,Restart=on-failure
- 配置文件 `config.json` 和 `.env` 在 `.gitignore` 中,**不要提交**

### 测试
- 必须用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`(系统装了 ROS launch_testing,会冲突)
- 测试不联网,外部依赖 mock 或用临时 db(`tmp_path` fixture)
- backtest_builtin 多线程非线程安全,测试用 `workers=1`

### Agent 设计
- **Function Calling ReAct**: LLM 自主决策调工具,失败降级到 `route()` 关键词路由
- **15 个 skill**: 4 数据查询 + 5 策略查询 + 3 策略操作 + 2 回测寻优 + 1 自选股
- **跨轮记忆**: `session_id = f"{chat_id}:{sender}"`,sqlite `agent_history.db`,最近 6 轮
  - assistant >500 字裁到 200 字 + 截断标记
  - >7 天自动过期(`_purge_old_history` 启动时清理)
- **重置命令**: 整句精确匹配(去标点),不用子串匹配(避免"重置 BOLL 参数"误判)
- **会话级并发锁**: 同 session_id 串行,防连发消息 race
- **参数 schema 预校验**: 对照 TOOLS schema,不合法直接回灌不执行
- **工具错误自愈**: 错误回灌"请用正确参数重试",LLM 自行修正
- **GOAP Scratchpad**: 复杂多步任务先写 Goal+Actions 规划
- **工具结果截断**: 超 3000 字截断,防上下文污染
- **图片缓存**: 同股同日 K 线图复用(_IMG_CACHE 30 张 LRU),玉姐图带 score 不缓存
- **LLM 重试**: 5xx/网络异常重试 1 次,4xx 不重试,失败给友好提示

### 操作类工具授权
- 用户明确表达意图(如"关闭 X 策略")才调用
- 模糊请求(如"看看策略")只调查询类
- LLM 智能判断,不需要硬编码规则

### 日志
- `RotatingFileHandler` `/tmp/feishu_bot.log` 5MB×3
- 同时输出控制台(journalctl 可查)
- 工具调用记 `Agent step N 调用 X(args)`

### 数据缓存
- `stock_cache.db`: 日 K 线(`daily` 表,~440 万行 405MB,正常)+ 玉姐精选 + AI 缓存 + 股票名(17 行)
- `agent_history.db`: 对话历史
- `agent_watchlist.db`: 自选股(按 session_id 隔离)
- 所有 `*.db` 在 `.gitignore` 中

### 策略大全(strategy_library.json)
- 4 来源: `book_cartoon`(漫画书 29) + `book_caolian`(操练大全 32) + `yujie_custom`(玉姐 10) + `ai_custom`(AI 2)
- 每个策略有 `id` / `name` / `category` / `implemented`(是否在引擎里实现)
- `cross_ref` 跨来源查同一策略在哪些书里出现

### 股票名识别(stock_names.py)
- 6 位代码直接返回
- 中文简称:腾讯 smartbox 搜索 + sqlite 缓存(1 天)
- 不预抓全市场,按需搜索
- 缓存全称 + 简称(去地名前缀)

## 已知问题

- `daily` 表 405MB(440 万行日 K 线): 设计如此,后续可加分区/归档
- lark-oapi ws 偶发 ping timeout: 已启用 auto_reconnect=True

## 不要做

- 不要用 python-dotenv(`ai_decider.load_env()` 手动解析 .env)
- 不要用 Docker(本地拉不到 python:3.11-slim,systemd 部署)
- 不要改 backtest_builtin 的 workers 默认值(非线程安全)
- 不要把 `config.json` / `.env` / `*.db` 提交到 git
- 不要在测试里联网(全部 mock 或 tmp_path)
