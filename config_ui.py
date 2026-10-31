"""WebUI 配置页的中文展示布局。

只重组 SDK 生成的 Schema（分块、标签、提示、页签），**不改配置模型、
不改默认值、也不改 TOML 里的真实路径**：每个展示块的 ``name`` 始终指向原有的配置节。

仿照 xxxxx7258.google-search-plugin 的做法：把「一个 TOML 节」拆成「常用 / 高级」
两块，让第一次打开配置页的人先看到真正要填的东西。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class _Section:
    """一个虚拟展示块。"""

    source: str
    title: str
    description: str
    fields: Dict[str, str]
    collapsed: bool = False


# 虚拟块 id -> 展示信息；顺序按下面 _TABS 里的排列
_SECTIONS: Dict[str, _Section] = {
    "mx": _Section(
        source="mx",
        title="妙想接口",
        description="插件靠它读模拟账户、持仓和委托，并真正下单。Key 在东方财富 App 的「妙想 Skills」页面领取。",
        fields={
            "api_key": "妙想 API Key",
            "base_url": "妙想服务地址",
            "timeout": "请求超时（秒）",
        },
    ),
    "notify": _Section(
        source="notify",
        title="播报目标",
        description="决定它把盈亏汇报到哪里。全部留空的话，它照常交易但不说话。",
        fields={
            "group_ids": "播报群号",
            "group_enabled": "启用群播报",
            "private_enabled": "启用私聊播报",
            "private_user_ids": "私聊用户 ID",
            "close_report_enabled": "收盘发一次复盘",
            "close_report_after": "复盘最早时间",
            "platform": "目标平台",
            "account_id": "限定账号 ID",
        },
    ),
    "owner": _Section(
        source="owner",
        title="主人白名单",
        description="只有名单里的人能改股票池和看盘节奏；查询类命令不受限制。",
        fields={"owners": "主人账号（platform:user_id）"},
    ),
    "account": _Section(
        source="account",
        title="账户与风控",
        description="这些是硬性纪律，代码里会真的重算一遍，不只是写在提示词里。",
        fields={
            "label": "账户显示名",
            "slippage": "限价让价比例",
            "max_position_ratio": "单票仓位上限",
            "min_cash_reserve_ratio": "现金底线",
            "max_trades_per_day": "每日下单上限",
            "buy_fee_rate": "买入费用率",
            "sell_fee_rate": "卖出费用率",
            "min_commission": "单笔最低佣金（元）",
        },
    ),
    "risk": _Section(
        source="risk",
        title="胆量档位",
        description="一款开关三档：稳健 / 激进 / 梭哈。它决定这台机器有多敢下手。",
        fields={
            "profile": "决策胆量",
            "position_multiplier": "单票仓位倍数（覆盖档位）",
            "cash_multiplier": "现金底线倍数（覆盖档位）",
            "trades_multiplier": "每日笔数倍数（覆盖档位）",
            "slippage_delta": "额外让价（覆盖档位）",
        },
    ),
    "schedule": _Section(
        source="schedule",
        title="看盘节奏",
        description="什么时候自动看盘、隔多久看一次。她每轮也能自己决定下次时间。",
        fields={
            "enabled": "启用自动看盘",
            "sessions": "交易时段",
            "default_interval_minutes": "默认间隔（分钟）",
            "min_interval_minutes": "最短间隔（分钟）",
            "max_interval_minutes": "最长间隔（分钟）",
            "timezone": "交易时段用的时区",
        },
    ),
    "prompt": _Section(
        source="prompt",
        title="决策提示词",
        description=(
            "她做交易判断时用的 system 提示词。必须保留 {header}、{max_position_pct}、{min_cash_pct} "
            "三个占位符，否则加载时直接报错——不会悄悄换回默认模板。"
        ),
        fields={"system_template": "提示词模板"},
        collapsed=True,
    ),
    "model": _Section(
        source="model",
        title="模型任务",
        description="复用麦麦已有的模型任务，不用在这里重复配接口。",
        fields={
            "decision_task_name": "决策用的模型任务",
            "decision_model": "决策用的具体模型",
            "report_task_name": "播报用的模型任务",
            "report_model": "播报用的具体模型",
        },
    ),
    "schedule_adv": _Section(
        source="schedule",
        title="节奏细节",
        description="后台空转与手动看盘的冷却，通常不用改。",
        fields={
            "idle_sleep_seconds": "空转检查间隔（秒）",
            "startup_delay_seconds": "启动后延迟（秒）",
            "manual_cooldown_seconds": "手动看盘冷却（秒）",
        },
        collapsed=True,
    ),
    "model_adv": _Section(
        source="model",
        title="模型参数",
        description="输出额度和超时。推理模型思考也占额度，给小了会只有思考没有正文。",
        fields={
            "temperature": "决策温度",
            "max_tokens": "决策输出上限",
            "report_max_tokens": "播报输出上限",
            "request_timeout_ms": "单次请求超时（毫秒）",
        },
        collapsed=True,
    ),
    "plugin_info": _Section(
        source="plugin",
        title="插件信息",
        description="用于版本识别，保持现状即可。",
        fields={"enabled": "启用插件", "config_version": "配置版本"},
        collapsed=True,
    ),
}

_TABS = {
    "basic": ("常用设置", ("mx", "notify", "owner")),
    "trading": ("交易", ("account", "risk", "schedule")),
    "model": ("模型", ("model",)),
    "advanced": ("高级", ("schedule_adv", "model_adv", "prompt", "plugin_info")),
}

# 覆盖 SDK 自动生成的描述：说明「填什么、为什么、填错会怎样」
_HINTS: Dict[str, str] = {
    "mx.api_key": (
        "在东方财富 App 首页搜「妙想 Skills」领取，mkt_v_ 开头；"
        "也可以改用环境变量 MX_APIKEY。别提交进仓库，插件也从不打印它。"
    ),
    "mx.base_url": "必须是 https://；不建议指向本机、内网或云元数据地址。",
    "mx.timeout": "单次请求妙想的超时；网络慢可以调大，不涉及行情接口。",
    "notify.group_ids": "要播报到哪些群，逐项填群号；留空就不往群里发。",
    "notify.group_enabled": "关掉后即使填了群号也不会往群里播报。",
    "notify.private_enabled": "私聊播报默认关闭；开启后需要同时填下面的用户 ID。",
    "notify.private_user_ids": "私聊播报发给谁，逐项填用户 ID。",
    "notify.close_report_after": "到这个时间之后才允许发当日复盘，格式 HH:MM。",
    "notify.platform": "平台标识，qq 就够；换平台才需要改。",
    "notify.account_id": "同一平台挂多个账号时，用账号 ID 消歧；留空表示不限。",
    "owner.owners": "能改股票池、调节奏的人，格式 platform:user_id，例如 qq:123456789；留空则只有查询类命令可用。",
    "account.label": "只影响播报里怎么称呼这个账户；留空则读妙想返回的组合名。",
    "account.slippage": "买入往上让价、卖出往下让价，保证挂在能成交的一侧。0.003 就是 0.3%。",
    "account.max_position_ratio": "单只股票最多占总资产多少；还会被「胆量档位」按倍数放大，代码里硬上限 40%。",
    "account.min_cash_reserve_ratio": "买入后至少留多少现金；梭哈档会把它压到很低。",
    "account.max_trades_per_day": "单日最多下几笔单，防上头；代码里硬上限 12 笔。",
    "account.buy_fee_rate": "买入佣金+过户费，默认 0.0006。只参与额度与仓位换算，真实扣费在妙想侧。",
    "account.sell_fee_rate": "卖出费用率（含印花税），默认 0.0011；小单卖出还不如拿着时会被拦下。",
    "account.min_commission": "券商单笔最低佣金，A 股普遍 5 元；填 0 表示不模拟。小单的真实成本靠它显形。",
    "risk.profile": (
        "steady 稳健 / aggressive 激进 / yolo 梭哈 / custom 自定义。"
        "档位在账户那一节的数字上乘倍数（单票 ×1/×1.5/×2.0，现金 ×1/×0.5/×0.15，"
        "笔数 ×1/×1.6/×2.0），代码里还有绝对上限，翻不过去。"
        "档位越高换手越勤、仓位越重，手续费全额模拟之后也越容易亏钱——想看节目效果就往上调。"
    ),
    "risk.position_multiplier": "只在 profile=custom 时生效：1.0 = 完全按 [account] 原值，不被放大。",
    "risk.cash_multiplier": "只在 profile=custom 时生效：越大越保守（现金底线越高），0 = 不留底线。",
    "risk.trades_multiplier": "只在 profile=custom 时生效：想少动手就填小一点。",
    "risk.slippage_delta": "只在 profile=custom 时生效：在 [account] slippage 之上再加的让价。",
    "prompt.system_template": (
        "她每轮做交易判断用的 system 提示词。必须保留 {header}、{max_position_pct}、"
        "{min_cash_pct} 三个占位符，{limits_notice} 可选；缺了会在加载时报错。"
    ),
    "schedule.sessions": "格式 HH:MM-HH:MM，可以写多段；只在这些时段内自动看盘。",
    "schedule.timezone": (
        "按哪个时区判断交易时段与跨日，例如 Asia/Shanghai；留空用宿主进程本地时区。"
        "在容器里跑 UTC 时必须填，否则它在 A 股开盘时会以为闭市、整天不开盘。"
    ),
    "schedule.min_interval_minutes": "她自己决定的间隔不会低于这个值。",
    "schedule.max_interval_minutes": "也不会高于这个值；调大可省妙想额度。",
    "schedule.manual_cooldown_seconds": "同一聊天流两次手动看盘（/股 试跑、review 工具）的最小间隔；设成 0 等于关掉冷却。",
    "model.decision_task_name": "默认 planner；换任务会影响决策用哪个模型。",
    "model.decision_model": "留空跟随任务；填了就用这个具体模型。",
    "model.report_task_name": "默认 replyer，播报的语气由它决定。",
    "model.report_model": "留空跟随任务。",
    "model.max_tokens": "决策输出上限；推理模型思考也占额度，给小了会「只有思考没有正文」。",
    "model.report_max_tokens": "播报输出的上限，一般不用动。",
    "model.request_timeout_ms": "单次模型调用允许等多久；思考型模型经常需要更长。",
    "plugin.config_version": "由插件自己维护，不要手改。",
}

# 只读字段：配置版本由插件管理，改错了会让宿主误判需要迁移
_DISABLED_FIELDS = {"plugin.config_version"}

# 需要按密码框渲染的字段
_PASSWORD_FIELDS = {"mx.api_key"}


def _build_field(source: str, name: str, template: Dict[str, Any], label: str, order: int) -> Dict[str, Any]:
    """在 SDK 给的字段定义上补中文标签、提示与顺序。"""

    path = f"{source}.{name}"
    entry = dict(template)
    entry.update(
        label=label,
        hint=_HINTS.get(path, template.get("description") or ""),
        description=_HINTS.get(path, template.get("description") or ""),
        order=order,
    )
    if path in _PASSWORD_FIELDS:
        entry["ui_type"] = "password"
    if path in _DISABLED_FIELDS:
        entry["disabled"] = True
    if path == "risk.profile":
        entry["choices"] = ["steady", "aggressive", "yolo", "custom"]
        entry["widget"] = "select"
    return entry


def build_config_ui_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """重组 SDK Schema：分出页签、中文标签、常用与高级分离。

    Args:
        schema: SDK ``build_config_schema()`` 生成的原始 Schema。

    Returns:
        Dict[str, Any]: 重组后的 Schema；字段默认值与类型保持原样。
    """

    source_sections: Dict[str, Any] = schema.get("sections") or {}
    sections: Dict[str, Any] = {}
    tabs: list[Dict[str, Any]] = []

    for tab_id, (tab_title, section_ids) in _TABS.items():
        tabs.append({"id": tab_id, "title": tab_title, "sections": list(section_ids)})
        for section_id in section_ids:
            spec = _SECTIONS[section_id]
            template_section = source_sections.get(spec.source)
            if template_section is None:
                continue
            template_fields = template_section.get("fields") or {}
            fields: Dict[str, Any] = {}
            for order, (field_name, label) in enumerate(spec.fields.items()):
                template = template_fields.get(field_name)
                if template is None:
                    continue
                fields[field_name] = _build_field(spec.source, field_name, template, label, order)
            if not fields:
                continue
            # 字典键只标识展示分块，name 才是 WebUI 读写配置的真实节名
            sections[section_id] = {
                **template_section,
                "name": spec.source,
                "title": spec.title,
                "description": spec.description,
                "collapsed": spec.collapsed,
                "order": len(sections),
                "fields": fields,
            }

    return {**schema, "sections": sections, "layout": {"type": "tabs", "tabs": tabs}}


__all__ = ["build_config_ui_schema"]
