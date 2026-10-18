"""命令参数解析：把「/股 xxx」后面那串东西宽松地认出来。

用户可能写代码、写名字、或者两个都写，顺序也不讲究：
    /股 行情 600036
    /股 招商银行
    /股 600036 招商银行
    /股 招商银行 600036
四种都当成同一件事：查这只票。
"""

from __future__ import annotations

import re

CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_RATE_PATTERN = re.compile(r"^(?P<minutes>\d{1,4})\s*(?:分钟|分|min|m)?$", re.IGNORECASE)

MAX_RATE_MINUTES = 480


class QueryParseError(ValueError):
    """参数无法解析。"""


def parse_stock_query(text: str) -> tuple[str, str]:
    """从任意文本里提取「六位代码」和「名字片段」，顺序不限。

    Returns:
        (code, name): 代码取第一个命中的六位数字，名字是去掉数字后剩下的部分。
    """

    raw = str(text or "").strip()
    codes = CODE_PATTERN.findall(raw)
    code = codes[0] if codes else ""
    name = " ".join(CODE_PATTERN.sub(" ", raw).split())
    return code, name


def parse_rate_argument(text: str) -> int:
    """解析「/股 节奏 15」里的分钟数；0 表示立刻看一次盘。"""

    raw = str(text or "").strip()
    match = _RATE_PATTERN.match(raw)
    if match is None:
        raise QueryParseError(f"节奏要写分钟数，比如「/股 节奏 15」，收到 {raw!r}")
    minutes = int(match.group("minutes"))
    if minutes < 0 or minutes > MAX_RATE_MINUTES:
        raise QueryParseError(f"分钟数要在 0~{MAX_RATE_MINUTES} 之间，收到 {minutes}")
    return minutes


__all__ = [
    "CODE_PATTERN",
    "MAX_RATE_MINUTES",
    "QueryParseError",
    "parse_rate_argument",
    "parse_stock_query",
]
