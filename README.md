# 麦麦模拟盘

<p>
  <a href="https://github.com/42yoru/maimai_sim_trader/actions/workflows/ci.yml"><img src="https://github.com/42yoru/maimai_sim_trader/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/Mai-with-u/MaiBot"><img src="https://img.shields.io/badge/MaiBot-1.3.9%2B-orange.svg" alt="MaiBot"></a>
  <a href="https://github.com/Mai-with-u/maibot-plugin-sdk"><img src="https://img.shields.io/badge/SDK-2.12.0%2B-green.svg" alt="SDK"></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python">
</p>

让麦麦用自己的判断去炒一个东方财富妙想模拟组合，然后在指定群或私聊里汇报盈亏。

- **账户与深度数据**：妙想 API（`mockTrading` 模拟组合 + `query` / `screen` 自然语言数据）
- **盘中报价**：腾讯行情快照（批量、免额度、快）
- **决策**：她自己的模型——买什么、买多少、多久看一次盘，都由她定
- **纪律**：价格换算、整手、仓位上限、现金底线、当日下单笔数上限写死在代码里
- **人设**：不写死任何角色，直接用宿主自己的昵称与人格

纯插件实现，不改主程序。

> 只做**模拟盘**。插件不接任何真实券商通道，亏赚都是虚拟的。

## 目录

- [她怎么工作](#她怎么工作) · [安装](#安装) · [配置](#配置) · [命令](#命令)
- [实际效果](#实际效果) · [常见问题](#常见问题) · [文件结构](#文件结构) · [已知边界](#已知边界)

## 她怎么工作

```
每轮（她决定下次隔多久）：
  妙想 balance / positions / orders  →  腾讯快照拉池子行情
      →  模型给出 JSON 决策（动作 / 池子增删 / 下次间隔 / 想说的话）
      →  代码按限价单规则下单
      →  有动作或她开口，就发到群里
收盘后：补一份当日复盘（每天一次）
```

妙想只收**限价单**，所以「想买」会换算成 `最新价 × (1 + slippage)`，「想卖」换成 `最新价 × (1 - slippage)`，保证挂在能成交的一侧。

## 安装

```bash
cd MaiBot/plugins
git clone https://github.com/42yoru/maimai_sim_trader.git
cp maimai_sim_trader/config.toml.example maimai_sim_trader/config.toml   # 可选，见下
```

**`config.toml` 不进仓库**（里面有 API Key），clone 下来是没有的。这一步其实可以省：直接启动 MaiBot，宿主会按插件声明的配置模型自动生成一份完整的 `config.toml`；手动 `cp` 只是为了拿到 `.example` 里的注释。

首次启动后日志里会给几条提示，照着补就行：

- `未配置妙想 API Key…后台循环不会启动` → 填 `[mx] api_key`（或设环境变量 `MX_APIKEY`），保存后循环自动开始
- `没有配置播报目标…` → 填 `[notify] group_ids`
- `owner.owners 为空…` → 填 `[owner] owners`，否则改股票池、调节奏的命令没人能用

**仓库里的默认值全是空的**，不预置任何群号或用户 ID，别人装上不会往作者那边发消息。

前置条件：妙想账号里已经有一个**模拟组合**，并且在妙想 Skills 页面把 API Key 绑定到它——否则 `mockTrading/*` 会返回 `404 请先绑定账户~`。

## 配置

`config.toml`：

| 节 | 关键项 | 说明 |
|---|---|---|
| `[mx]` | `api_key` / `base_url` | 妙想 API Key（`mkt_v_` 开头），**别提交到仓库**；留空读环境变量 `MX_APIKEY`。`base_url` 必须是 `https://`，不建议指向本机/内网地址 |
| `[account]` | `slippage` / `max_position_ratio` / `min_cash_reserve_ratio` / `max_trades_per_day` | 让价、单票上限、现金底线、当日笔数上限 |
| `[schedule]` | `sessions` / `min_interval_minutes` / `max_interval_minutes` | 交易时段与她自选间隔的合法范围 |
| `[model]` | `decision_task_name` / `report_task_name` / `max_tokens` | 默认决策用 `planner`、播报用 `replyer`；`max_tokens` 默认 4000，思考型模型给小了会只有思考没有正文 |
| `[notify]` | `group_ids` / `private_user_ids` / `private_enabled` | 默认全空：不填就不播报；私聊功能已实现但默认关 |
| `[owner]` | `owners` | 能改股票池、调节奏的人，格式 `qq:123456789`；留空时只有查询类命令可用 |

**人设不在插件里配。** 启动时从宿主读 `bot.nickname`（自称）和 `personality.personality`（人格），决策和播报都用你自己 bot 的那一套——插件本身不带任何角色设定。宿主没配人设时退回通用称呼「麦麦」并在日志里警告。

## 命令

**查询类（所有人可用）**

| 命令 | 作用 |
|---|---|
| `/股 状态` | 总资产、可用资金、持仓、盈亏 |
| `/股 池` | 股票池清单 |
| `/股 委托` | 当日委托与状态 |
| `/股 记录` | 今天看了几轮、最近的心情和判断 |
| `/股 试跑` | 跑一轮决策但**不下单**，只回报打算做什么 |
| `/股 600036` | 查单只票实时价 |

行情查询是宽松识别的，下面四种写法等价，顺序随便：

```
/股 行情 600036
/股 招商银行
/股 600036 招商银行
/股 招商银行 600036
```

只写名字时会在股票池里找；池子里没有就请写六位代码。

**改动类（只有 `[owner] owners` 里的人能用）**

| 命令 | 作用 |
|---|---|
| `/股 加 600519 贵州茅台` | 加一只进池子 |
| `/股 减 600519` | 踢一只出池子 |
| `/股 节奏 15` | 15 分钟后再看一次盘；`/股 节奏 0` 表示立刻看 |

## 工具（自然语言触发）

聊天里她可以直接用这两个工具，不用敲命令：

| 工具 | 说明 |
|---|---|
| `sim_trader_query` | `action` 取 `status` / `pool` / `orders` / `history` / `quote`，分别是账户、池子、委托、最近判断、单只票行情 |
| `sim_trader_review` | 让她**此刻重新看一次盘并自行决定买不买**，跑完把心情、判断和动作回报给她。交易时段内**会真实下单到模拟盘**，闭市时自动降级为只看不做 |

所以群里有人喊「麦麦梭哈茅台」，麦麦可以自己决定要不要调 `sim_trader_review`——听不听、动不动，都是它自己的判断，插件只提供能力。工具触发的这一轮**不会额外播报**，结果回到聊天里由它自己讲。

**这两个工具都没有权限门槛**：`sim_trader_query` 只是读数据，`sim_trader_review` 会让它跑一整轮决策。交易时段内可能真的下模拟单，闭市时只算不做；同一聊天流两次手动看盘之间有冷却（`[schedule] manual_cooldown_seconds`，默认 60 秒），避免有人在群里反复刷、白烧模型的 token。

真下单仍然受 `max_trades_per_day`、单票上限、现金底线约束。

## 落盘

数据目录（`ctx.paths.data_dir`）下：

- `watchlist.json`：股票池，她可以自己加减
- `state.json`：当日计数、下次看盘时间、最近几轮记录、今日是否已复盘

两个文件都是原子写（临时文件 + `os.replace`）；文件损坏直接抛错，不静默清空。

## 实际效果

一轮决策的真实输出（planner 模型）：

```json
{
  "mood": "手有点痒但克制",
  "reasoning": "首轮空仓，指数涨跌互现属平淡震荡。选池中趋势最强、量能温和的三只各建约10%仓位试水，留七成现金。",
  "actions": [
    {"action": "buy", "code": "601899", "name": "紫金矿业", "quantity": 3400, "reason": "放量走强"},
    {"action": "buy", "code": "600309", "name": "万华化学", "quantity": 1500, "reason": "领涨池内"},
    {"action": "buy", "code": "601138", "name": "工业富联", "quantity": 2100, "reason": "量能温和"}
  ],
  "pool_change": {"add": [], "remove": []},
  "next_check_minutes": 60,
  "speech": "先伸一只脚试试水"
}
```

代码换算成限价单后的测算：

```
紫金矿业 3400股 @30.01 = 102,034 元 (10.2%)
万华化学 1500股 @66.44 =  99,660 元 (10.0%)
工业富联 2100股 @47.92 = 100,632 元 (10.1%)
合计 30.2%，剩余现金 69.8%
```

群里收到的播报（replyer 模型）：

> 主人～群里汇报一下哈：现在账户总资产一百万整，可用资金也是一百万，还空着仓，一分没赚一分没亏，净值1.0000，仓位0%。手是真的有点痒……不过我想好了，第一次先拿三成去试试水。紫金矿业买3400股，30.01；万华化学1500股，66.44；工业富联2100股，47.92，各占一成左右，剩下七成现金先攥着，不乱来 (｡•́︿•̀｡) 等真建了仓再跟大家报盈亏，现在先忍着。

## 开盘前自检

```bash
python scripts/preflight.py
```

一次确认四件事：妙想 key 还有效、组合能读、池子里每只票都拿得到价、播报群在 MaiBot 数据库里唯一。
任何一项失败会返回非零退出码并说明原因；全程不打印密钥。

## 常见问题

**`404 请先绑定账户~`**
妙想账号里还没有把 API Key 绑定到模拟组合。去 [妙想 Skills 页面](https://dl.dfcfs.com/m/itc4) 找到「妙想模拟组合管理」，在「绑定组合名称」里填上组合名，点绑定。绑完再跑 `python scripts/preflight.py` 验证。

**试跑报「模型只输出了思考过程没有正文」**
决策模型的 `max_tokens` 太小，思考阶段就把额度用光了。把 `[model] max_tokens` 调大（默认 4000）。

**群里收不到播报**
依次检查：`[notify] group_enabled` 是否为 true、`group_ids` 里的群号是否正确、`account_id` 留空是否匹配到了多个账号。日志里会有 `找不到群 xxx 的聊天流` 或 `命中 N 条聊天流` 的提示。

**她好几天都不交易**
这是设计允许的——规则里写明「没机会就不动」。用 `/股 记录` 看她最近几轮的判断，`/股 节奏 15` 可以让她更勤快地看盘。

**播报语气不像我的 bot**
插件从宿主读 `bot.nickname` 和 `personality.personality`。如果宿主没配人设，会退回通用称呼并在日志里警告。

## 文件结构

```
maimai_sim_trader/
├── plugin.py           # 插件入口：配置模型、后台循环、命令、工具
├── mx_client.py        # 妙想 HTTP 客户端（账户 / 委托 / 下单 / 查询 / 选股）
├── quotes.py           # 腾讯行情快照，批量拉价并解析
├── positions.py        # 持仓字段适配（字段名有变体，认不出代码就报错）
├── trading.py          # 限价换算、整手、仓位与现金约束
├── decision.py         # 决策 prompt 与严格 JSON 校验
├── report.py           # 播报文案（语气归模型，数字归代码）
├── watchlist.py        # 股票池，原子写
├── state.py            # 运行状态：节奏、计数、最近记录
├── query_args.py       # 命令参数的宽松识别
├── fmt.py              # 金额 / 百分比 / 盈亏格式化
├── scripts/preflight.py# 开盘前自检
├── tests/              # 单测，不碰网络
├── config.toml.example
└── _manifest.json
```

## 已知边界

- 妙想**不支持市价单**；深市 ETF（`159` 开头）不支持。
- 数量必须 100 的整数倍，A 股 T+1，当日买的不能当日卖。
- 模拟组合须在妙想 Skills 页面绑定后 `mockTrading/*` 才可用，否则返回 `404 请先绑定账户~`。
- 决策模型每轮一次调用；她可以把间隔拉到 240 分钟来省额度。
- 插件不接任何真实券商通道，纯模拟盘。
- **两个 HTTP 客户端都关掉了 `trust_env`**（`mx_client.py` / `quotes.py`），即**不走系统代理和 `HTTP(S)_PROXY` 环境变量**。这是为了躲开畸形代理变量导致 httpx 建 URL 崩溃的老问题。部署环境如果必须通过代理才能出网，妙想和腾讯行情都会连不通，需要给主机配透明代理，或把 `[mx] base_url` 指到可直连的地址。

## 测试

```bash
cd plugins/maimai_sim_trader
python -m unittest discover -s tests -t .
```

单测不碰网络，只验限价换算、整手、资金/仓位约束、决策解析、池子增删、持仓字段适配、命令参数识别、主人校验和整轮流程编排。

## 贡献

欢迎提 Issue 和 PR。提交前请确保：

```bash
ruff check . --select E,F,W,B --line-length 130
python -m unittest discover -s tests -t .
```

都通过。带密钥的 `config.toml`、`config_back/` 和 `data/` 已在 `.gitignore` 里，别提交上去。

## 许可证

[MIT](LICENSE)