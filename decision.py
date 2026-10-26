"""决策层：把账户、行情、池子拼成 prompt，让模型吐出结构化决策。

模型只负责「判断」和「表达」，价格换算、整手、风控全部交给 trading.py。
所以这里的解析必须严格：格式不对就抛错跳轮，绝不猜模型的意图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

import json
import re

from .fmt import UNKNOWN, account_profit, money, pct, price, ratio, to_float
from .positions import Holding

MAX_ACTIONS = 3
MIN_CHECK_MINUTES = 10
MAX_CHECK_MINUTES = 240
DEFAULT_CHECK_MINUTES = 30
MAX_REASON_CHARS = 120
MAX_SPEECH_CHARS = 200
VALID_ACTIONS = ("buy", "sell")

_CODE_PATTERN = re.compile(r"^\d{6}$")


class DecisionParseError(ValueError):
    """模型输出不符合预期结构。"""


@dataclass(frozen=True)
class TradeAction:
    """一条交易意图。"""

    action: str
    code: str
    name: str
    quantity: int
    reason: str


@dataclass(frozen=True)
class PoolChange:
    """池子增删意图。"""

    add: Tuple[Tuple[str, str], ...] = ()
    remove: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """一轮决策的完整结果。"""

    mood: str
    reasoning: str
    actions: Tuple[TradeAction, ...] = ()
    pool_change: PoolChange = field(default_factory=PoolChange)
    next_check_minutes: int = DEFAULT_CHECK_MINUTES
    speech: str = ""


SYSTEM_PROMPT = """{header}
你现在在替主人打理一个 A 股模拟盘账户。
你有自己的情绪：赚了会得意，亏了会难受，看到好机会会兴奋——但下单必须讲纪律。

规则（必须遵守）：
1. 只能买卖股票池里的股票；想买池子外的票，先把它加进池子再说。
2. A 股 T+1，当天买的不能当天卖；数量必须是 100 股的整数倍。
3. 单票仓位不超过总资产的 {max_position_pct}，且至少要留 {min_cash_pct} 现金——除非你在清仓，否则不要满仓。
   【这一轮的额度】里写的是这一轮实际还剩多少空间和笔数，按那个来，不要自己估。
4. 可以什么都不做。没机会就不动，"今天不想动"是一个完全体面的答案。
5. 每轮只做真正的判断，不要为了显勤奋而交易。
6. 你可以自己决定下次多久后再看盘（next_check_minutes，10~240 分钟）：行情剧烈就早点回来，平淡就久一点。
7. 已经连续空仓好几天的话，这一轮要么动手建仓，要么用一句话讲清你到底在等什么——不许含糊过去。

只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块：
{
  "mood": "现在的情绪，8 字以内，比如 平静/有点得意/有点慌",
  "reasoning": "这轮的判断依据，80 字以内",
  "actions": [
    {"action": "buy 或 sell", "code": "六位代码", "name": "股票名", "quantity": 100 的整数倍, "reason": "为什么动它，40 字以内"}
  ],
  "pool_change": {"add": [{"code": "六位代码", "name": "股票名"}], "remove": ["六位代码"]},
  "next_check_minutes": 30,
  "speech": "如果想在群里说一句话就说，没有就留空字符串"
}
actions 最多 3 条，可以是空数组。pool_change 的 add/remove 没有就留空数组。"""


def _coerce_int(value: Any, *, field_name: str) -> int:
    """把模型给的数字转成 int；转不了就报错。"""

    if isinstance(value, bool):
        raise DecisionParseError(f"{field_name} 不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise DecisionParseError(f"{field_name} 不是整数: {value!r}")


def _position_pct_text(balance: Dict[str, Any], positions: Sequence[Holding]) -> str:
    """当前仓位；妙想偶尔不给 totalPosPct，就用持仓市值自己算。

    额度块里的仓位是用持仓市值算的，这里若退回「未知」，
    同一个 prompt 里就会出现「仓位 未知」和「现在 41.3%」并存的怪事。
    """

    reported = pct(balance.get("totalPosPct"))
    if reported != UNKNOWN:
        return reported
    total_assets = to_float(balance.get("totalAssets"))
    if total_assets <= 0:
        return UNKNOWN
    held_value = sum(item.market_value for item in positions)
    return f"{held_value / total_assets * 100:.2f}%"


def _clean_text(value: Any, *, limit: int) -> str:
    """压成一行纯文本并截断。"""

    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:limit]


def _extract_json_object(text: str) -> str:
    """从模型输出里截出最外层 JSON 对象。"""

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise DecisionParseError(f"输出里找不到 JSON 对象: {text[:200]!r}")
    return text[start : end + 1]


def _parse_actions(raw: Any) -> Tuple[TradeAction, ...]:
    """解析交易意图列表。"""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DecisionParseError(f"actions 不是数组: {type(raw).__name__}")
    if len(raw) > MAX_ACTIONS:
        raise DecisionParseError(f"actions 最多 {MAX_ACTIONS} 条，收到 {len(raw)} 条")
    actions: List[TradeAction] = []
    for row in raw:
        if not isinstance(row, dict):
            raise DecisionParseError(f"actions 条目不是对象: {row!r}")
        action = str(row.get("action") or "").strip().lower()
        if action not in VALID_ACTIONS:
            raise DecisionParseError(f"action 只能是 {'/'.join(VALID_ACTIONS)}，收到 {action!r}")
        code = str(row.get("code") or "").strip()
        if not _CODE_PATTERN.match(code):
            raise DecisionParseError(f"股票代码必须是六位数字，收到 {code!r}")
        quantity = _coerce_int(row.get("quantity"), field_name="quantity")
        if quantity <= 0:
            raise DecisionParseError(f"quantity 必须大于 0，收到 {quantity}")
        actions.append(
            TradeAction(
                action=action,
                code=code,
                name=_clean_text(row.get("name"), limit=20) or code,
                quantity=quantity,
                reason=_clean_text(row.get("reason"), limit=MAX_REASON_CHARS),
            )
        )
    return tuple(actions)


def _parse_pool_change(raw: Any) -> PoolChange:
    """解析池子增删。"""

    if raw is None:
        return PoolChange()
    if not isinstance(raw, dict):
        raise DecisionParseError(f"pool_change 不是对象: {type(raw).__name__}")
    add_rows = raw.get("add") or []
    remove_rows = raw.get("remove") or []
    if not isinstance(add_rows, list) or not isinstance(remove_rows, list):
        raise DecisionParseError("pool_change.add / remove 必须是数组")
    added: List[Tuple[str, str]] = []
    for row in add_rows:
        if not isinstance(row, dict):
            raise DecisionParseError(f"pool_change.add 条目不是对象: {row!r}")
        code = str(row.get("code") or "").strip()
        if not _CODE_PATTERN.match(code):
            raise DecisionParseError(f"新增股票代码必须是六位数字，收到 {code!r}")
        added.append((code, _clean_text(row.get("name"), limit=20) or code))
    removed: List[str] = []
    for row in remove_rows:
        code = str(row or "").strip()
        if not _CODE_PATTERN.match(code):
            raise DecisionParseError(f"移除股票代码必须是六位数字，收到 {code!r}")
        removed.append(code)
    return PoolChange(add=tuple(added), remove=tuple(removed))


def parse_decision(text: str) -> Decision:
    """把模型输出解析成 Decision；结构不对直接抛错。"""

    payload_text = _extract_json_object(text)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(payload, dict):
        raise DecisionParseError("决策结果不是 JSON 对象")

    check_minutes = _coerce_int(payload.get("next_check_minutes", DEFAULT_CHECK_MINUTES), field_name="next_check_minutes")
    check_minutes = max(MIN_CHECK_MINUTES, min(MAX_CHECK_MINUTES, check_minutes))
    return Decision(
        mood=_clean_text(payload.get("mood"), limit=16) or "平静",
        reasoning=_clean_text(payload.get("reasoning"), limit=200),
        actions=_parse_actions(payload.get("actions")),
        pool_change=_parse_pool_change(payload.get("pool_change")),
        next_check_minutes=check_minutes,
        speech=_clean_text(payload.get("speech"), limit=MAX_SPEECH_CHARS),
    )


def build_messages(
    *,
    now_text: str,
    session_label: str,
    balance: Dict[str, Any],
    positions: Sequence[Holding],
    pool_lines: Sequence[str],
    pending_orders: Sequence[str],
    history_blocks: Sequence[str],
    limits_block: Sequence[str] = (),
    idle_note: str = "",
    bot_name: str = "麦麦",
    persona: str = "",
    risk_style: str = "",
    max_position_pct: str = "20%",
    min_cash_pct: str = "20%",
) -> List[Dict[str, str]]:
    """拼出本轮决策用的 messages。

    Args:
        limits_block: 这一轮实际还能动多少（单票余量、总仓位余量、剩余笔数），
            由调用方算好数字；空着就不渲染这一块。
        risk_style: 当前风险档的风格提示（稳健/激进/梭哈），留空则不注入。
        max_position_pct / min_cash_pct: 由配置与风险档算出的实际限制，
            直接写进 system prompt，避免 prompt 里的数字和代码里的校验对不上。
    """

    balance_lines = [
        f"总资产 {money(balance.get('totalAssets'))}",
        f"可用资金 {money(balance.get('availBalance'))}",
        f"持仓市值 {money(balance.get('totalPosValue'))}",
        f"累计盈亏 {account_profit(balance)}",
        f"净值 {ratio(balance.get('nav'))}",
        f"仓位 {_position_pct_text(balance, positions)}",
    ]
    position_lines = [item.to_prompt_line(money=money, price=price) for item in positions] or ["（空仓）"]
    quota_lines = ["【这一轮的额度】", *limits_block, ""] if limits_block else []

    user_text = "\n".join(
        [
            f"现在是 {now_text}，账户「{session_label}」。",
            "",
            "【账户】",
            *balance_lines,
            *([f"持仓状态：{idle_note}"] if idle_note else []),
            "",
            "【持仓】",
            *position_lines,
            "",
            *quota_lines,
            "【股票池与最新行情】",
            *(pool_lines or ["（池子是空的，先加几只）"]),
            "",
            "【未成交委托】",
            *(pending_orders or ["（无）"]),
            "",
            "【最近几轮（越靠下越新，接着上面的思路想，不要每轮重新开始）】",
            *(history_blocks or ["（这是第一轮）"]),
            "",
            "请判断这一轮要不要动，并按格式输出 JSON。",
        ]
    )
    header = persona.strip() or f"你是{bot_name}。"
    system_text = (
        SYSTEM_PROMPT.replace("{header}", header)
        .replace("{max_position_pct}", max_position_pct)
        .replace("{min_cash_pct}", min_cash_pct)
    )
    if risk_style.strip():
        system_text = f"{system_text}\n\n【这一轮的风格】\n{risk_style.strip()}"
    return [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]


__all__ = [
    "DEFAULT_CHECK_MINUTES",
    "MAX_ACTIONS",
    "MAX_CHECK_MINUTES",
    "MIN_CHECK_MINUTES",
    "Decision",
    "DecisionParseError",
    "PoolChange",
    "TradeAction",
    "build_messages",
    "parse_decision",
]
