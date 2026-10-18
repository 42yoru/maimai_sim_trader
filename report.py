"""播报：把账户和决策变成 bot 自己的话。

群里发言要短，私聊可以细一点；语气归模型，数字归代码。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .fmt import account_profit, money, money_plain, pct, ratio
from .positions import Holding

MAX_GROUP_CHARS = 220
MAX_PRIVATE_CHARS = 600

REPORT_SYSTEM_PROMPT = """{header}
你在替主人炒一个 A 股模拟盘账户，现在要向{where}汇报盘面情况。

怎么写：
1. 用你自己的口吻，像真的在跟人说话，不要写报表。
2. 盈亏是你的真实情绪来源：赚了可以得意，亏了可以嘴硬、可以闷闷不乐，但不要编造数字。
3. 数字必须和给的完全一致，不要自己算新的。
4. 不要提"模型""插件""系统"这些词，你就是那个在炒股的人。
5. {limit_rule}
6. 不要用表情包占位符；颜文字可以用一两个。

直接输出要发送的文本，不要任何前缀或解释。"""


def build_snapshot_lines(balance: Dict[str, Any], positions: Sequence[Holding]) -> List[str]:
    """账户快照的纯文本摘要。"""

    lines = [
        f"总资产 {money(balance.get('totalAssets'))}",
        f"可用资金 {money(balance.get('availBalance'))}",
        f"持仓市值 {money(balance.get('totalPosValue'))}",
        f"累计盈亏 {account_profit(balance)}",
        f"净值 {ratio(balance.get('nav'))}",
        f"仓位 {pct(balance.get('totalPosPct'))}",
    ]
    if positions:
        lines.append("持仓明细：")
        for item in positions:
            lines.append(
                f"  {item.code} {item.name} {item.quantity}股 "
                f"成本{money_plain(item.avg_cost)} 现价{money_plain(item.current_price)} "
                f"盈亏{money(item.profit)}"
            )
    else:
        lines.append("当前空仓。")
    return lines


def build_report_messages(
    *,
    bot_name: str,
    persona: str = "",
    where: str,
    group_style: bool,
    snapshot_lines: Sequence[str],
    round_lines: Sequence[str],
    mood: str,
    reasoning: str,
) -> List[Dict[str, str]]:
    """拼出播报用的 messages。"""

    header = persona.strip() or f"你是{bot_name}。"
    if group_style:
        limit_rule = f"整段话控制在 {MAX_GROUP_CHARS} 字以内，像在群里随手发的一条消息。"
        system = REPORT_SYSTEM_PROMPT.format(header=header, where=where, limit_rule=limit_rule)
    else:
        system = REPORT_SYSTEM_PROMPT.format(
            header=header,
            where=where,
            limit_rule=f"可以详细一点，但不超过 {MAX_PRIVATE_CHARS} 字。",
        )
    user_text = "\n".join(
        [
            "【账户现状】",
            *snapshot_lines,
            "",
            "【这一轮的判断】",
            f"心情：{mood or '平静'}",
            f"理由：{reasoning or '（没写）'}",
            *round_lines,
        ]
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]


def build_plain_fallback(*, snapshot_lines: Sequence[str], mood: str, reasoning: str) -> str:
    """不经过模型的极简播报，用于模型调用失败时把事实说清楚。"""

    parts = [f"【{mood or '平静'}】{reasoning}".strip(), *snapshot_lines[:4]]
    return "\n".join(part for part in parts if part)


__all__ = [
    "MAX_GROUP_CHARS",
    "MAX_PRIVATE_CHARS",
    "REPORT_SYSTEM_PROMPT",
    "build_plain_fallback",
    "build_report_messages",
    "build_snapshot_lines",
]
