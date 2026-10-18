"""数字与文本格式化：金额、百分比、盈亏推算。

decision 和 report 都要把同一笔账户数据说成人话，格式和口径放在这里统一。
"""

from __future__ import annotations

from typing import Any, Dict

UNKNOWN = "未知"


def to_float(value: Any, *, default: float = 0.0) -> float:
    """尽量取浮点；取不到给默认值。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def money(value: Any) -> str:
    """金额：千分位 + 两位小数。"""

    if value is None or value == "":
        return UNKNOWN
    try:
        return f"{float(value):,.2f} 元"
    except (TypeError, ValueError):
        return UNKNOWN


def money_plain(value: Any) -> str:
    """金额但不带单位，适合表格里拼。"""

    if value is None or value == "":
        return "?"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "?"


def price(value: Any) -> str:
    """价格：两位小数。"""

    if value is None or value == "":
        return UNKNOWN
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return UNKNOWN


def ratio(value: Any) -> str:
    """净值：四位小数。"""

    if value is None or value == "":
        return UNKNOWN
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return UNKNOWN


def pct(value: Any, *, signed: bool = False) -> str:
    """百分比。妙想返回的多是 0~100 的数值。"""

    if value is None or value == "":
        return UNKNOWN
    try:
        number = float(value)
    except (TypeError, ValueError):
        return UNKNOWN
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def account_profit(balance: Dict[str, Any]) -> str:
    """累计盈亏。

    空仓或刚开户时妙想不给 totalProfit，这时用「总资产 - 初始资金」算，
    口径和账户详情页一致，不是拍出来的数。
    """

    total_profit = balance.get("totalProfit")
    if total_profit not in (None, ""):
        return money(total_profit)
    total_assets = to_float(balance.get("totalAssets"))
    init_money = to_float(balance.get("initMoney"))
    if init_money <= 0:
        return UNKNOWN
    return money(total_assets - init_money)


def compact(text: str) -> str:
    """压掉多余空白（腾讯的股票名里带全角空格）。"""

    return " ".join(str(text).split())


__all__ = [
    "UNKNOWN",
    "account_profit",
    "compact",
    "money",
    "money_plain",
    "pct",
    "price",
    "ratio",
    "to_float",
]
