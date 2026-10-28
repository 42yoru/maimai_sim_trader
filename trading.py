"""下单规则：限价、整手、资金与仓位约束。

妙想只收限价单，所以「想买」必须换算成一个大概率能成交的限价。
所有换算和风控都集中在这里，决策层只负责提交意图（买谁、多少、为什么）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LOT_SIZE = 100
MIN_PRICE_STEP = 0.01
# 买入佣金+过户费，卖出再加印花税；数值取偏保守的一侧，宁可少买一点
BUY_FEE_RATE = 0.0006
SELL_FEE_RATE = 0.0011
# 券商单笔最低佣金。以前完全没有模拟它，小单的真实成本被系统性低估，看起来总是"赚"
DEFAULT_MIN_COMMISSION = 0.0


def estimated_fee(gross_amount: float, *, fee_rate: float, min_commission: float = 0.0) -> float:
    """估算单边交易费用：比例费与最低佣金取大者。"""

    if gross_amount <= 0:
        return 0.0
    return max(gross_amount * fee_rate, min_commission)


def round_price(price: float) -> float:
    """对齐到 A 股最小变动单位 0.01 元。"""

    return round(price + 1e-9, 2)


def limit_price(latest_price: float, *, direction: str, slippage: float) -> float:
    """按最新价和滑点算限价。

    买入往上让、卖出往下让——限价单挂在「愿意成交」的一侧才可能吃到。
    """

    if latest_price <= 0:
        raise ValueError(f"最新价必须大于 0，收到 {latest_price}")
    if direction == "buy":
        ratio = 1.0 + slippage
    elif direction == "sell":
        ratio = 1.0 - slippage
    else:
        raise ValueError(f"direction 只能是 buy/sell，收到 {direction!r}")
    return max(MIN_PRICE_STEP, round_price(latest_price * ratio))


def round_lots(quantity: int) -> int:
    """向下取整到整手。"""

    if quantity <= 0:
        return 0
    return int(quantity // LOT_SIZE) * LOT_SIZE


def max_affordable_quantity(
    *,
    price: float,
    available_cash: float,
    buy_fee_rate: float = BUY_FEE_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
) -> int:
    """按可用资金算出最多能买几手（含手续费与最低佣金）。"""

    if price <= 0 or available_cash <= 0:
        return 0
    per_lot_cost = price * LOT_SIZE + estimated_fee(
        price * LOT_SIZE, fee_rate=buy_fee_rate, min_commission=min_commission
    )
    if per_lot_cost <= 0:
        return 0
    return round_lots(int(available_cash // per_lot_cost) * LOT_SIZE)


@dataclass(frozen=True)
class TradeCheck:
    """一次下单前的风控结论。"""

    ok: bool
    reason: str = ""

    @classmethod
    def pass_(cls) -> "TradeCheck":
        """通过。"""

        return cls(ok=True)

    @classmethod
    def reject(cls, reason: str) -> "TradeCheck":
        """拒绝，并带上人话原因。"""

        return cls(ok=False, reason=reason)


def check_buy(
    *,
    quantity: int,
    price: float,
    available_cash: float,
    total_assets: float,
    current_position_value: float,
    max_position_ratio: float,
    min_cash_reserve_ratio: float,
    buy_fee_rate: float = BUY_FEE_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
) -> TradeCheck:
    """买入前的资金、整手、仓位与现金底线检查。"""

    if quantity <= 0 or quantity % LOT_SIZE != 0:
        return TradeCheck.reject(f"数量必须大于 0 且是 {LOT_SIZE} 的整数倍，收到 {quantity}")
    cost = price * quantity + estimated_fee(
        price * quantity, fee_rate=buy_fee_rate, min_commission=min_commission
    )
    if cost > available_cash:
        return TradeCheck.reject(f"可用资金不足：需要约 {cost:.2f} 元，只有 {available_cash:.2f} 元")
    target_position_value = current_position_value + quantity * price
    if total_assets > 0 and target_position_value / total_assets > max_position_ratio:
        return TradeCheck.reject(
            f"单票仓位会到 {target_position_value / total_assets:.1%}，超过上限 {max_position_ratio:.0%}"
        )
    remain_cash = available_cash - cost
    if total_assets > 0 and remain_cash / total_assets < min_cash_reserve_ratio:
        return TradeCheck.reject(
            f"买完只剩 {remain_cash / total_assets:.1%} 现金，低于保留底线 {min_cash_reserve_ratio:.0%}"
        )
    return TradeCheck.pass_()


def check_sell(
    *,
    quantity: int,
    available_quantity: int,
    price: float = 0.0,
    sell_fee_rate: float = SELL_FEE_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
) -> TradeCheck:
    """卖出前的整手、可用数量与费用检查。"""

    if quantity <= 0 or quantity % LOT_SIZE != 0:
        return TradeCheck.reject(f"数量必须大于 0 且是 {LOT_SIZE} 的整数倍，收到 {quantity}")
    if quantity > available_quantity:
        return TradeCheck.reject(f"可卖数量不足：想卖 {quantity} 股，只有 {available_quantity} 股可卖")
    gross = price * quantity
    fee = estimated_fee(gross, fee_rate=sell_fee_rate, min_commission=min_commission)
    if gross > 0 and gross - fee <= 0:
        # 卖出侧以前完全不看费用，小单会出现"卖掉还不如拿着"
        return TradeCheck.reject(f"这笔只值 {gross:.2f} 元，扣掉卖出费用 {fee:.2f} 元后不剩什么")
    return TradeCheck.pass_()


def position_cap_quantity(*, price: float, total_assets: float, current_position_value: float, max_position_ratio: float) -> int:
    """在单票仓位上限内，还能再加多少股。"""

    if price <= 0 or total_assets <= 0:
        return 0
    ceiling_value = total_assets * max_position_ratio
    headroom = ceiling_value - current_position_value
    if headroom <= 0:
        return 0
    return round_lots(int(headroom // price))


def resolve_quantity(
    *,
    requested: int,
    price: float,
    available_cash: float,
    total_assets: float,
    current_position_value: float,
    max_position_ratio: float,
    min_cash_reserve_ratio: float,
    buy_fee_rate: float = BUY_FEE_RATE,
    min_commission: float = DEFAULT_MIN_COMMISSION,
) -> Optional[int]:
    """把模型给的股数收敛成能真正下单的股数；无法收敛返回 None。

    优先按请求量取整手；资金或仓位不够就向下压缩；压到 0 就是「不买」。
    """

    quantity = round_lots(requested)
    if quantity <= 0:
        return None
    affordable = max_affordable_quantity(
        price=price,
        available_cash=available_cash,
        buy_fee_rate=buy_fee_rate,
        min_commission=min_commission,
    )
    if affordable <= 0:
        return None
    quantity = min(quantity, affordable)
    cap = position_cap_quantity(
        price=price,
        total_assets=total_assets,
        current_position_value=current_position_value,
        max_position_ratio=max_position_ratio,
    )
    quantity = min(quantity, cap)
    while quantity > 0:
        check = check_buy(
            quantity=quantity,
            price=price,
            available_cash=available_cash,
            total_assets=total_assets,
            current_position_value=current_position_value,
            max_position_ratio=max_position_ratio,
            min_cash_reserve_ratio=min_cash_reserve_ratio,
            buy_fee_rate=buy_fee_rate,
            min_commission=min_commission,
        )
        if check.ok:
            return quantity
        quantity -= LOT_SIZE
    return None


__all__ = [
    "BUY_FEE_RATE",
    "DEFAULT_MIN_COMMISSION",
    "LOT_SIZE",
    "MIN_PRICE_STEP",
    "SELL_FEE_RATE",
    "TradeCheck",
    "check_buy",
    "check_sell",
    "estimated_fee",
    "limit_price",
    "max_affordable_quantity",
    "position_cap_quantity",
    "resolve_quantity",
    "round_lots",
    "round_price",
]
