"""纯函数单测：不碰网络，只验规则和解析。

插件模块按 Runner 的方式（importlib + 子模块搜索路径）加载，
这样 plugin.py 里的相对导入才可用。
"""

from __future__ import annotations

from typing import Any

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "maimai_sim_trader"


def _load_plugin_module() -> Any:
    """按 Runner 的加载方式导入插件包。"""

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        str(PLUGIN_DIR / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法为插件创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_load_plugin_module()

DECISION = sys.modules[f"{MODULE_NAME}.decision"]
STATE = sys.modules[f"{MODULE_NAME}.state"]
TRADING = sys.modules[f"{MODULE_NAME}.trading"]
WATCHLIST = sys.modules[f"{MODULE_NAME}.watchlist"]
POSITIONS = sys.modules[f"{MODULE_NAME}.positions"]

DecisionParseError = DECISION.DecisionParseError
parse_decision = DECISION.parse_decision
RuntimeState = STATE.RuntimeState
StateStore = STATE.StateStore
check_sell = TRADING.check_sell
limit_price = TRADING.limit_price
max_affordable_quantity = TRADING.max_affordable_quantity
resolve_quantity = TRADING.resolve_quantity
round_lots = TRADING.round_lots
DEFAULT_POOL = WATCHLIST.DEFAULT_POOL
WatchlistStore = WATCHLIST.WatchlistStore
parse_holdings = POSITIONS.parse_holdings


class TradingTests(unittest.TestCase):
    """限价、整手、资金约束。"""

    def test_limit_price_rounds_to_cent(self) -> None:
        self.assertEqual(limit_price(10.0, direction="buy", slippage=0.003), 10.03)
        self.assertEqual(limit_price(10.0, direction="sell", slippage=0.003), 9.97)

    def test_limit_price_rejects_bad_input(self) -> None:
        with self.assertRaises(ValueError):
            limit_price(0, direction="buy", slippage=0.003)
        with self.assertRaises(ValueError):
            limit_price(10, direction="hold", slippage=0.003)

    def test_round_lots_floors_to_hand(self) -> None:
        self.assertEqual(round_lots(199), 100)
        self.assertEqual(round_lots(200), 200)
        self.assertEqual(round_lots(0), 0)

    def test_max_affordable_quantity(self) -> None:
        # 10 元 * 100 股 * (1+0.0006) ≈ 1000.6，1000 元只够 0 手
        self.assertEqual(max_affordable_quantity(price=10.0, available_cash=1000.0), 0)
        self.assertEqual(max_affordable_quantity(price=10.0, available_cash=1001.0), 100)

    def test_check_sell_rejects_odd_lot_and_over_sell(self) -> None:
        self.assertFalse(check_sell(quantity=150, available_quantity=500).ok)
        self.assertFalse(check_sell(quantity=600, available_quantity=500).ok)
        self.assertTrue(check_sell(quantity=500, available_quantity=500).ok)

    def test_resolve_quantity_shrinks_to_fit(self) -> None:
        """要 10 万股但撞上单票仓位上限时，应被压到上限那一档。"""

        quantity = resolve_quantity(
            requested=100000,
            price=10.0,
            available_cash=900000.0,
            total_assets=1000000.0,
            current_position_value=0.0,
            max_position_ratio=0.2,
            min_cash_reserve_ratio=0.2,
        )
        self.assertEqual(quantity, 20000)

    def test_resolve_quantity_respects_cash(self) -> None:
        """现金不够时按能买得起的量收敛，并且不跌破现金底线。"""

        quantity = resolve_quantity(
            requested=20000,
            price=10.0,
            available_cash=1000000.0,
            total_assets=1000000.0,
            current_position_value=0.0,
            max_position_ratio=0.9,
            min_cash_reserve_ratio=0.2,
        )
        self.assertIsNotNone(quantity)
        assert quantity is not None
        self.assertEqual(quantity % 100, 0)
        self.assertLessEqual(quantity * 10.0, 800000.0)

    def test_resolve_quantity_returns_none_when_hopeless(self) -> None:
        quantity = resolve_quantity(
            requested=100,
            price=10.0,
            available_cash=100.0,
            total_assets=1000000.0,
            current_position_value=0.0,
            max_position_ratio=0.2,
            min_cash_reserve_ratio=0.2,
        )
        self.assertIsNone(quantity)


class DecisionTests(unittest.TestCase):
    """模型输出解析。"""

    def test_parse_full_decision(self) -> None:
        payload = {
            "mood": "有点得意",
            "reasoning": "银行板块量能起来了",
            "actions": [{"action": "buy", "code": "600036", "name": "招商银行", "quantity": 300, "reason": "突破"}],
            "pool_change": {"add": [{"code": "601988", "name": "中国银行"}], "remove": ["600660"]},
            "next_check_minutes": 15,
            "speech": "刚才那波挺漂亮",
        }
        decision = parse_decision(f"```json\n{json.dumps(payload, ensure_ascii=False)}\n```")
        self.assertEqual(decision.mood, "有点得意")
        self.assertEqual(len(decision.actions), 1)
        self.assertEqual(decision.actions[0].quantity, 300)
        self.assertEqual(decision.pool_change.remove, ("600660",))
        self.assertEqual(decision.next_check_minutes, 15)

    def test_parse_clamps_interval(self) -> None:
        decision = parse_decision('{"mood":"平静","reasoning":"","actions":[],"next_check_minutes":9999}')
        self.assertEqual(decision.next_check_minutes, 240)

    def test_parse_rejects_bad_action(self) -> None:
        with self.assertRaises(DecisionParseError):
            parse_decision('{"actions":[{"action":"hold","code":"600036","quantity":100}]}')

    def test_parse_rejects_bad_code(self) -> None:
        with self.assertRaises(DecisionParseError):
            parse_decision('{"actions":[{"action":"buy","code":"60036","quantity":100}]}')

    def test_parse_rejects_non_json(self) -> None:
        with self.assertRaises(DecisionParseError):
            parse_decision("我觉得今天不太行")


class WatchlistTests(unittest.TestCase):
    """股票池增删与落盘。"""

    def test_default_pool_created_on_first_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            items = store.all()
            self.assertEqual(len(items), len(DEFAULT_POOL))
            self.assertTrue((Path(tmp) / "watchlist.json").exists())

    def test_add_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            store.load()
            self.assertTrue(store.add("600519", "贵州茅台"))
            self.assertFalse(store.add("600519", "贵州茅台"))
            removed = store.remove("600519")
            self.assertIsNotNone(removed)
            assert removed is not None
            self.assertEqual(removed.name, "贵州茅台")
            self.assertIsNone(store.remove("600519"))

    def test_corrupted_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(WATCHLIST.WatchlistError):
                WatchlistStore(path).load()


class HoldingTests(unittest.TestCase):
    """持仓字段适配：字段名一变就得认出来，认不出就报错。"""

    def test_standard_shape(self) -> None:
        holdings = parse_holdings(
            [
                {
                    "stockCode": "600036",
                    "stockName": "招商银行",
                    "quantity": 500,
                    "availableQty": 500,
                    "avgCost": 41.2,
                    "currentPrice": 42.45,
                    "marketValue": 21225.0,
                    "profit": 625.0,
                }
            ]
        )
        holding = holdings["600036"]
        self.assertEqual(holding.name, "招商银行")
        self.assertEqual(holding.quantity, 500)
        self.assertEqual(holding.available, 500)
        self.assertAlmostEqual(holding.avg_cost, 41.2)
        self.assertAlmostEqual(holding.market_value, 21225.0)

    def test_alternative_field_names(self) -> None:
        holdings = parse_holdings(
            [
                {
                    "code": "000001",
                    "name": "平安银行",
                    "holdAmount": "300",
                    "availableQuantity": "200",
                    "costPrice": "11.10",
                    "lastPrice": "11.50",
                }
            ]
        )
        holding = holdings["000001"]
        self.assertEqual(holding.quantity, 300)
        self.assertEqual(holding.available, 200)
        self.assertAlmostEqual(holding.current_price, 11.50)
        # marketValue 缺失时用现价 * 数量兜住
        self.assertAlmostEqual(holding.market_value, 3450.0)

    def test_missing_code_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_holdings([{"stockName": "某票", "quantity": 100}])

    def test_missing_available_is_zero(self) -> None:
        """可卖数量拿不到时按 0 处理——宁可卖不出去，也不凭猜数下单。"""

        holdings = parse_holdings([{"stockCode": "600036", "quantity": 500}])
        self.assertEqual(holdings["600036"].available, 0)


class StateTests(unittest.TestCase):
    """状态落盘与跨日重置。"""

    def test_roll_date_resets_counters(self) -> None:
        state = RuntimeState(trade_date="2026-10-18", check_count=3, trade_count=2)
        self.assertTrue(state.roll_date("2026-10-19"))
        self.assertEqual(state.check_count, 0)
        self.assertEqual(state.trade_count, 0)
        self.assertFalse(state.roll_date("2026-10-19"))

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            state = store.load()
            state.record_round(note="10:00 没动", mood="平静", reasoning="等着", trade_count=0)
            state.next_check_at = 123.0
            store.save()
            reloaded = StateStore(path).load()
            self.assertEqual(reloaded.check_count, 1)
            self.assertEqual(reloaded.recent_notes, ["10:00 没动"])
            self.assertEqual(reloaded.next_check_at, 123.0)

    def test_empty_days_tracking(self) -> None:
        state = RuntimeState()
        state.track_position_state(has_position=False, today="2026-10-19")
        self.assertEqual(state.empty_since, "2026-10-19")
        self.assertEqual(state.empty_days("2026-10-19"), 1)
        self.assertEqual(state.empty_days("2026-10-22"), 4)
        state.track_position_state(has_position=False, today="2026-10-23")
        self.assertEqual(state.empty_since, "2026-10-19")

    def test_empty_days_cleared_after_buying(self) -> None:
        state = RuntimeState()
        state.track_position_state(has_position=False, today="2026-10-19")
        state.track_position_state(has_position=True, today="2026-10-20")
        self.assertEqual(state.empty_since, "")
        self.assertEqual(state.empty_days("2026-10-21"), 0)
        state.track_position_state(has_position=False, today="2026-10-22")
        self.assertEqual(state.empty_since, "2026-10-22")


if __name__ == "__main__":
    unittest.main()
