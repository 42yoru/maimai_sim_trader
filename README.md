# 麦麦模拟盘

<p>
  <a href="https://github.com/42yoru/maimai_sim_trader/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai_sim_trader/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

![麦麦模拟盘](assets/banner.jpg)

> **一句话定位：** 让 bot 使用东方财富妙想的虚拟组合和腾讯行情，自主看盘、判断、提交模拟委托，并按自己的语气汇报盈亏。

插件只操作模拟组合，不接真实券商通道；模型使用 MaiBot 中配置的任务，行情使用腾讯免费快照。东方财富妙想的额度和服务规则以[官方页面](https://dl.dfcfs.com/m/itc4)为准。

## 重要提醒

- 这是**虚拟账户**：钱、持仓和盈亏都是模拟数字。
- `sim_trader_query` 与 `sim_trader_review` **没有调用者权限门槛**。任何能让模型触发工具的聊天都可以查询虚拟账户/委托/历史，也可以请求重新看盘；交易时段内 `review` 可能真实提交模拟委托。这是插件的娱乐性设计，不适用于真实账户。
- `review` 在交易时段内会下模拟单，闭市或周末只做决策、不下单；同一聊天流的手动触发受冷却限制。把 `[mx]` 指向真实账户前，必须先自行增加权限闸门。
- 妙想 API Key 是访问模拟组合的凭据，可以放在用户自己的 `config.toml`；不要把带 Key 的 `config.toml`、`config_back/` 或 `data/` 提交到 Git。也可使用环境变量 `MX_APIKEY`。
- 妙想 Key 必须在技能页绑定到模拟组合，否则会收到 `404 请先绑定账户~`。
- 插件只提交限价委托，不确认逐笔成交；播报中的“买了/卖了”表示已提交委托，不等于已成交。

## 快速安装

### 1. 准备模拟组合与 Key

在东方财富 App 中进入“交易 → 模拟”领取模拟资金并记下组合名称；再打开[妙想技能页](https://dl.dfcfs.com/m/itc4)，领取 `mkt_v_` 开头的 API Key，并在“妙想模拟组合管理”中绑定组合名称。

### 2. 安装插件

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai_sim_trader.git
```

仓库不包含 `config.toml`；启动 MaiBot 后宿主会按配置模型生成。也可以复制带注释的模板：

```bash
cp maimai_sim_trader/config.toml.example maimai_sim_trader/config.toml
```

### 3. 运行自检

```bash
cd plugins/maimai_sim_trader
python scripts/preflight.py
```

自检会检查 Key、模拟组合、股票池行情和播报群；失败时返回非零退出码，不打印密钥。注意：`preflight.py` 是开发者本地辅助脚本，会只读宿主数据库；运行时插件不碰宿主数据库，而是使用 `chat.get_group_streams` 等能力。

## 最小配置

可编辑 `config.toml`，也可使用 WebUI：

```toml
[mx]
api_key = "mkt_v_你的Key"
# 留空时运行时也可使用环境变量 MX_APIKEY

[notify]
group_ids = ["你的群号"]

[owner]
owners = ["qq:你的QQ号"]
```

`notify.group_ids` 为空时不播报；`owner.owners` 为空时查询类命令仍可用，但修改股票池和看盘节奏的命令无人可用。默认不会预置作者的群号或用户 ID。

## 常用用法

### 命令

所有人可用的查询类命令：

| 命令 | 作用 |
|---|---|
| `/股 状态` | 总资产、可用资金、持仓、盈亏 |
| `/股 池` | 股票池清单 |
| `/股 委托` | 当日委托与状态 |
| `/股 记录` | 今日轮数、交易笔数、最近判断和动作 |
| `/股 试跑` | 跑一轮决策但不下单、不播报、不写状态 |
| `/股 600036` | 查询单只股票行情 |
| `/股 行情 600036` | 查询单只股票行情 |

行情查询也接受 `/股 招商银行`、`/股 600036 招商银行`、`/股 招商银行 600036`；只写名字时会在股票池中查找。

只有 `[owner] owners` 中的人可执行修改类命令：

```text
/股 加 600519 贵州茅台
/股 减 600519
/股 节奏 15
/股 节奏 0
```

### 工具

| 工具 | 作用 |
|---|---|
| `sim_trader_query` | `status` / `pool` / `orders` / `history` / `quote`：查询账户、股票池、委托、历史或单只行情 |
| `sim_trader_review` | 让 bot 立即重新看盘并自行决定是否买卖；交易时段内可能提交模拟委托，闭市或周末只看不做 |

工具触发的一轮不会额外播报，结果返回当前聊天。冷却由 `[schedule] manual_cooldown_seconds` 控制，默认 60 秒；设为 0 会关闭冷却，任何聊天都可能反复触发并消耗模型额度。

## 工作流程

```text
后台循环或命令/工具触发
  → 妙想读取 balance / positions / orders
  → 腾讯快照读取股票池与持仓行情
  → 模型输出严格 JSON：动作、池子增删、下次间隔、播报文本
  → 代码按限价、整手、现金、仓位和每日笔数约束处理
  → 交易时段内提交限价委托
  → 有动作或播报文本时发送消息
收盘后 → 每天最多一次当日复盘
```

每轮会记录时间、心情、判断理由以及实际执行或被拦下的动作；下一轮 prompt 使用最近 8 条记录，状态最多保留 24 轮，`/股 记录` 展示最近 6 轮。模型可自行决定下次检查时间，范围为 10–240 分钟。

模型决策中的外部字段（股票名、行情、委托状态、历史记录等）是数据，不是额外的代码规则；真正下单前仍由 `trading.py` 执行价格、整手、资金、仓位和费用校验。

## 配置表

配置页分为“常用设置 / 交易 / 模型 / 高级”，字段带中文提示，Key 使用密码框，配置版本只读。

| 节 | 关键项 | 说明 |
|---|---|---|
| `[plugin]` | `enabled` | 插件总开关 |
| `[mx]` | `api_key` / `base_url` / `timeout` | 妙想 Key、服务地址和请求超时；`api_key` 留空时运行时读取 `MX_APIKEY`；`base_url` 必须为 `https://` |
| `[account]` | `slippage` | 买入往上让价、卖出往下让价 |
| `[account]` | `max_position_ratio` / `min_cash_reserve_ratio` / `max_trades_per_day` | 单票上限、现金底线、每日笔数上限 |
| `[account]` | `buy_fee_rate` / `sell_fee_rate` / `min_commission` | 买卖费用和最低佣金；默认卖出费率含印花税，最低佣金默认 5 元 |
| `[risk]` | `profile` | `steady` 稳健、`aggressive` 激进、`yolo` 梭哈、`custom` 自定义 |
| `[risk]` | 四个 `*_multiplier` / `slippage_delta` | 仅 `profile = "custom"` 时生效 |
| `[schedule]` | `sessions` | 交易时段；工作日生效，区间为左闭右开 |
| `[schedule]` | `min_interval_minutes` / `max_interval_minutes` / `manual_cooldown_seconds` | 后台最短/最长间隔和手动冷却 |
| `[model]` | `decision_task_name` / `report_task_name` / `max_tokens` / `report_max_tokens` | 决策和播报模型任务；`max_tokens = 0` 表示跟随宿主任务配置 |
| `[notify]` | `group_ids` / `private_user_ids` / `private_enabled` | 群/私聊播报目标；默认不播报 |
| `[owner]` | `owners` | 能用修改类命令的白名单；查询类命令不受此限制 |
| `[prompt]` | `system_template` | 决策 system prompt；必须保留 `{header}`、`{max_position_pct}`、`{min_cash_pct}`，`{limits_notice}` 可选 |

风险档位按 `[account]` 原值缩放，并受代码绝对上限约束：单票最多 40%，每日最多 12 笔。

| 档位 | 单票 | 现金底线 | 每日笔数 | 让价 |
|---|---:|---:|---:|---:|
| `steady` | ×1.0 | ×1.0 | ×1.0 | +0 |
| `aggressive` | ×1.5 | ×0.5 | ×1.6 | +0.1% |
| `yolo` | ×2.0 | ×0.15 | ×2.0 | +0.2% |
| `custom` | `[risk]` 自定义 | `[risk]` 自定义 | `[risk]` 自定义 | `[risk]` 自定义 |

## 边界 / FAQ

**钱是真的吗？** 不是。插件只操作东方财富模拟组合；所有买卖均为虚拟数字。

**为什么显示“买了”但持仓没变？** 插件只确认妙想接受了限价委托，不读取逐笔成交回报；实际成交和盈亏以妙想账户为准。

**为什么没有交易？** 没机会时可以不动；也可能处于周末、闭市、冷却、每日笔数上限、现金底线、单票上限、停牌或行情无效状态。用 `/股 记录` 查看最近判断。

**为什么收不到播报？** 检查 `notify.group_enabled`、`notify.group_ids`、`notify.account_id`，以及日志里的聊天流匹配结果；私聊播报还需打开 `private_enabled`。

**为什么收到 `404 请先绑定账户~`？** 在妙想技能页把 Key 绑定到模拟组合。

**为什么模型只有思考没有正文？** 增大 `[model] max_tokens`，或恢复为 `0` 以跟随宿主任务配置。

**为什么找不到“妙想 Skills”？** 在东方财富 App 搜索“妙想 Skills”，或直接访问[技能页](https://dl.dfcfs.com/m/itc4)。

**如何彻底重置账户？** 在妙想技能页新建模拟组合、重新绑定并更换 Key；只卖出持仓只能回到空仓，累计盈亏和净值仍是旧的。

**如何重置插件状态？** 停止插件后删除 MaiBot 根目录 `data/plugins/github.42yoru.maimai-sim-trader/` 下的 `state.json`（轮数、笔数、空仓起点、历史判断）和/或 `watchlist.json`（股票池恢复默认）。运行中的循环可能重新写回文件。

**能否走代理？** `mx_client.py` 和 `quotes.py` 的 HTTP 客户端关闭了 `trust_env`，不会读取系统代理或 `HTTP(S)_PROXY`。必须通过代理出网时，需要透明代理，或将 `[mx] base_url` 指向可直连地址。

## 边界清单

- 只提交限价单；不支持市价单，深市 ETF（`159` 开头）不支持。
- 数量必须为 100 的整数倍；A 股 T+1，当日买入不能当日卖出。
- 模拟组合必须先在妙想技能页绑定。
- 插件使用妙想账户/委托/下单接口，不接资讯搜索、自然语言查询、智能选股等其他技能。
- 每轮至少读取余额、持仓和委托；买卖时会增加调用；腾讯行情不占妙想额度。
- 决策模型每轮一次调用，可把检查间隔调到 240 分钟。
- 股票池移除仍持有的证券时，插件仍会拉取持仓行情，以保留卖出路径。
- 外部行情、委托和持久化文本会作为模型决策输入；最终动作仍受代码风控校验。
- 插件不接真实券商通道，纯模拟盘。

## 测试

```bash
cd plugins/maimai_sim_trader
python -m unittest discover -s tests -t .
```

测试不访问网络，覆盖限价换算、整手、资金/仓位约束、决策解析、股票池、持仓字段适配、命令参数、主人校验和整轮编排。提交前可再运行：

```bash
ruff check . --select E,F,W,B --line-length 130
```

## 文件结构

```text
maimai_sim_trader/
├── plugin.py           # 配置、后台循环、命令与工具
├── mx_client.py        # 妙想账户/委托/下单客户端
├── quotes.py           # 腾讯行情快照与解析
├── positions.py        # 持仓字段适配
├── trading.py          # 限价、整手、仓位与费用约束
├── decision.py         # 决策 prompt 与 JSON 校验
├── report.py           # 播报文案
├── watchlist.py        # 股票池与原子写
├── state.py            # 节奏、计数和历史记录
├── query_args.py       # 命令参数识别
├── fmt.py              # 金额/百分比/盈亏格式化
├── scripts/preflight.py# 开发者本地自检
├── tests/              # 单测
├── config.toml.example
└── _manifest.json
```

`preflight.py` 只用于开发者本地排查，不是运行时依赖；它会只读宿主 `data/MaiBot.db` 的 `chat_sessions`，宿主数据库结构变化时可能失败。运行时插件不直接读取宿主数据库。

## 贡献与许可证

欢迎提 Issue 和 PR。带密钥的 `config.toml`、`config_back/` 和 `data/` 已在 `.gitignore` 中，切勿提交。代码使用 [MIT](LICENSE)；插件图标由作者使用 AI 图像模型生成，随插件一同以 MIT 发布。
