"""持仓字段适配。

妙想不同接口/版本里持仓字段名不一致（stockCode / code、availableQty / availableQuantity…），
而账户空仓时根本拿不到真实样本来核对。统一在这里解析成 Holding：

- 股票代码是识别持仓的唯一键，拿不到就直接报错（协议变了，不能装作没事）；
- 其余字段缺失按 0 处理并记 warning，宁可卖不出去，也不能凭猜数去下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import logging

logger = logging.getLogger(__name__)

CODE_KEYS = ("stockCode", "secCode", "code", "symbol")
NAME_KEYS = ("stockName", "secName", "name", "shortName")
QUANTITY_KEYS = ("quantity", "count", "holdAmount", "stockQty", "holdQty")
AVAILABLE_KEYS = (
    "availCount",
    "availableQuantity",
    "availableQty",
    "sellableQuantity",
    "enableAmount",
    "enableQty",
    "availableStockQty",
    "canSellQty",
)
COST_KEYS = ("avgCost", "costPrice", "averageCost", "cost")
PRICE_KEYS = ("currentPrice", "price", "lastPrice", "nowPrice")
VALUE_KEYS = ("value", "marketValue", "marketVal", "holdMarketValue", "positionValue", "posValue")
PROFIT_KEYS = ("profit", "floatProfit", "profitLoss", "income", "dayProfit")
# 妙想的价格/成本是「定点整数 + 小数位」，要按 *Dec 缩放成真实价格
COST_DEC_KEYS = ("costPriceDec", "costDec")
PRICE_DEC_KEYS = ("priceDec", "lastPriceDec")


def _first_present(row: Dict[str, Any], keys: Iterable[str]) -> Any:
    """按候选顺序取第一个有值的字段。"""

    for key in keys:
        value = row.get(key)
        if value not in (None, "", "--"):
            return value
    return None


def _scaled_amount(row: Dict[str, Any], keys: Iterable[str], dec_keys: Iterable[str]) -> float:
    """取定点数并按小数位缩放；没有 Dec 字段说明本来就是小数，原样返回。"""

    raw = _first_present(row, keys)
    if raw is None:
        return 0.0
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return 0.0
    decimals = _first_present(row, dec_keys)
    if decimals is None:
        return number
    try:
        return number / (10 ** int(decimals))
    except (TypeError, ValueError, OverflowError):
        return number


def _as_int(value: Any) -> int:
    """尽量转整数。"""

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """尽量转浮点。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Holding:
    """一只持仓的标准视图。"""

    code: str
    name: str
    quantity: int
    available: int
    avg_cost: float
    current_price: float
    market_value: float
    profit: float

    def to_prompt_line(self, *, money: Any, price: Any) -> str:
        """拼成给模型看的一行。"""

        return (
            f"{self.code} {self.name} 持有{self.quantity}股 可卖{self.available}股 "
            f"成本{price(self.avg_cost)} 现价{price(self.current_price)} 盈亏{money(self.profit)}"
        )


def parse_holdings(rows: Iterable[Dict[str, Any]]) -> Dict[str, Holding]:
    """把妙想持仓列表解析成「代码 → Holding」。"""

    holdings: Dict[str, Holding] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"持仓条目不是对象: {row!r}")
        raw_code = _first_present(row, CODE_KEYS)
        if raw_code in (None, ""):
            raise ValueError(f"持仓条目缺少股票代码，字段名可能变了: {sorted(row.keys())}")
        code = str(raw_code).strip()
        name = str(_first_present(row, NAME_KEYS) or code).strip()
        quantity = _as_int(_first_present(row, QUANTITY_KEYS))
        raw_available = _first_present(row, AVAILABLE_KEYS)
        available = _as_int(raw_available)
        if quantity > 0 and raw_available is None:
            # 字段完全缺失才值得警告；值为 0 是 T+1 的正常状态
            logger.warning(
                "持仓 %s 有 %d 股但找不到可卖数量字段，字段名可能变了（现有键：%s）",
                code,
                quantity,
                sorted(row.keys()),
            )
        avg_cost = _scaled_amount(row, COST_KEYS, COST_DEC_KEYS)
        current_price = _scaled_amount(row, PRICE_KEYS, PRICE_DEC_KEYS)
        market_value = _as_float(_first_present(row, VALUE_KEYS))
        profit = _as_float(_first_present(row, PROFIT_KEYS))
        if quantity > 0 and available <= 0:
            logger.warning(
                "持仓 %s 数量 %d 但可卖数量解析为 0，字段名可能变了（现有键：%s）",
                code,
                quantity,
                sorted(row.keys()),
            )
        if market_value <= 0 and current_price > 0:
            market_value = current_price * quantity
        holdings[code] = Holding(
            code=code,
            name=name,
            quantity=quantity,
            available=available,
            avg_cost=avg_cost,
            current_price=current_price,
            market_value=market_value,
            profit=profit,
        )
    return holdings


def total_market_value(holdings: Dict[str, Holding]) -> float:
    """持仓总市值。"""

    return sum(item.market_value for item in holdings.values())


def find_holding(holdings: Dict[str, Holding], code: str) -> Optional[Holding]:
    """按代码查持仓。"""

    return holdings.get(code.strip())


__all__ = [
    "AVAILABLE_KEYS",
    "CODE_KEYS",
    "Holding",
    "find_holding",
    "parse_holdings",
    "total_market_value",
]
