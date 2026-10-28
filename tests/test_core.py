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
QUERY_ARGS = sys.modules[f"{MODULE_NAME}.query_args"]
QUOTES = sys.modules[f"{MODULE_NAME}.quotes"]
PLUGIN_MODULE = sys.modules[MODULE_NAME]

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
parse_snapshot = QUOTES.parse_snapshot
parse_stock_query = QUERY_ARGS.parse_stock_query
parse_rate_argument = QUERY_ARGS.parse_rate_argument
QueryParseError = QUERY_ARGS.QueryParseError


class QueryArgsTests(unittest.TestCase):
    """命令参数的宽松识别。"""

    def test_code_only(self) -> None:
        self.assertEqual(parse_stock_query("行情 600036"), ("600036", "行情"))

    def test_name_only(self) -> None:
        self.assertEqual(parse_stock_query("招商银行"), ("", "招商银行"))

    def test_code_then_name(self) -> None:
        self.assertEqual(parse_stock_query("600036 招商银行"), ("600036", "招商银行"))

    def test_name_then_code(self) -> None:
        self.assertEqual(parse_stock_query("招商银行 600036"), ("600036", "招商银行"))

    def test_ignores_longer_numbers(self) -> None:
        """7 位数不该被当成代码。"""

        self.assertEqual(parse_stock_query("6000367"), ("", "6000367"))

    def test_rate_argument(self) -> None:
        self.assertEqual(parse_rate_argument("15"), 15)
        self.assertEqual(parse_rate_argument("30分钟"), 30)
        self.assertEqual(parse_rate_argument("0"), 0)
        with self.assertRaises(QueryParseError):
            parse_rate_argument("快点")
        with self.assertRaises(QueryParseError):
            parse_rate_argument("9999")


class WatchlistNameTests(unittest.TestCase):
    """按名字找票。"""

    def test_exact_and_partial_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            store.load()
            self.assertEqual(store.find_by_name("招商银行").code, "600036")
            self.assertEqual(store.find_by_name("招商").code, "600036")

    def test_unknown_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            store.load()
            self.assertIsNone(store.find_by_name("不存在的股票"))

    def test_ambiguous_partial(self) -> None:
        """部分匹配命中多只时返回 None，让用户写代码。"""

        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            store.load()
            store.add("600519", "贵州茅台")
            self.assertIsNone(store.find_by_name("中"))


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


class DecisionPromptTests(unittest.TestCase):
    """决策 prompt 里的风控数字必须来自配置，不能写死。"""

    @staticmethod
    def _messages(**overrides):
        kwargs = dict(
            now_text="09:35",
            session_label="测试组合",
            balance={"totalAssets": 1000000, "availBalance": 900000},
            positions=[],
            pool_lines=["601899 紫金矿业"],
            pending_orders=[],
            history_blocks=[],
        )
        kwargs.update(overrides)
        return DECISION.build_messages(**kwargs)

    def test_default_limits_are_20_percent(self) -> None:
        system_text = self._messages()[0]["content"]
        self.assertIn("单票仓位不超过总资产的 20%", system_text)
        self.assertIn("至少要留 20% 现金", system_text)

    def test_limits_come_from_caller(self) -> None:
        """配置改成 35% / 5% 时，prompt 里不能还是 20%。"""

        system_text = self._messages(max_position_pct="35%", min_cash_pct="5%")[0]["content"]
        self.assertIn("单票仓位不超过总资产的 35%", system_text)
        self.assertIn("至少要留 5% 现金", system_text)
        self.assertNotIn("不超过总资产的 20%", system_text)

    def test_risk_style_is_appended(self) -> None:
        system_text = self._messages(risk_style="这一轮按梭哈档来。")[0]["content"]
        self.assertIn("【这一轮的风格】", system_text)
        self.assertIn("梭哈档", system_text)

    def test_no_risk_style_keeps_prompt_clean(self) -> None:
        system_text = self._messages()[0]["content"]
        self.assertNotIn("【这一轮的风格】", system_text)

    def test_history_blocks_are_injected(self) -> None:
        """上一轮的判断和动作必须进这一轮的 prompt。"""

        block = STATE.RoundRecord(
            at="10-24 14:20",
            mood="谨慎",
            reasoning="缩量阴跌，先不动",
            actions=["想买 600519，但资金不够，没买"],
        ).to_prompt_block()
        user_text = self._messages(history_blocks=[block])[1]["content"]
        self.assertIn("【最近几轮", user_text)
        self.assertIn("10-24 14:20｜心情：谨慎", user_text)
        self.assertIn("判断：缩量阴跌，先不动", user_text)
        self.assertIn("动作：想买 600519，但资金不够，没买", user_text)

    def test_empty_history_says_first_round(self) -> None:
        user_text = self._messages(history_blocks=[])[1]["content"]
        self.assertIn("（这是第一轮）", user_text)

    def test_limits_block_is_rendered(self) -> None:
        user_text = self._messages(limits_block=["- 今天已下 3 笔，上限 12 笔，还能下 9 笔"])[1]["content"]
        self.assertIn("【这一轮的额度】", user_text)
        self.assertIn("还能下 9 笔", user_text)

    def test_limits_block_is_optional(self) -> None:
        user_text = self._messages()[1]["content"]
        self.assertNotIn("【这一轮的额度】", user_text)

    def test_position_pct_uses_reported_value(self) -> None:
        user_text = self._messages(balance={"totalAssets": 500000.0, "totalPosPct": 12.5})[1]["content"]
        self.assertIn("仓位 12.50%", user_text)

    def test_position_pct_falls_back_to_holdings(self) -> None:
        """妙想不给 totalPosPct 时用持仓市值算，别让 prompt 里一半是「未知」。"""

        holding = POSITIONS.Holding(
            code="600519",
            name="贵州茅台",
            quantity=100,
            available=100,
            avg_cost=1.0,
            current_price=2000.0,
            market_value=200000.0,
            profit=0.0,
        )
        user_text = self._messages(
            balance={"totalAssets": 500000.0, "availBalance": 300000.0},
            positions=[holding],
        )[1]["content"]
        self.assertIn("仓位 40.00%", user_text)
        self.assertNotIn("仓位 未知", user_text)

    def test_position_pct_zero_with_holdings_falls_back(self) -> None:
        """有持仓却报 0.00%，多半是字段没同步，也按持仓市值算。"""

        holding = POSITIONS.Holding(
            code="600519",
            name="贵州茅台",
            quantity=100,
            available=100,
            avg_cost=1.0,
            current_price=2000.0,
            market_value=200000.0,
            profit=0.0,
        )
        user_text = self._messages(
            balance={"totalAssets": 500000.0, "totalPosPct": 0},
            positions=[holding],
        )[1]["content"]
        self.assertIn("仓位 40.00%", user_text)

    def test_no_limits_notice_without_block(self) -> None:
        """没渲染额度块时，规则里也不该提到它。"""

        system_text = self._messages()[0]["content"]
        self.assertNotIn("【这一轮的额度】", system_text)

    def test_limits_notice_present_with_block(self) -> None:
        system_text = self._messages(limits_block=["- 今天还能下 3 笔"])[0]["content"]
        self.assertIn("【这一轮的额度】", system_text)


class LimitsBlockTests(unittest.TestCase):
    """额度块：数字必须来自实际持仓和当日笔数。"""

    @staticmethod
    def _holding(code: str, name: str, market_value: float) -> Any:
        return POSITIONS.Holding(
            code=code,
            name=name,
            quantity=100,
            available=100,
            avg_cost=1.0,
            current_price=1.0,
            market_value=market_value,
            profit=0.0,
        )

    def test_block_reports_remaining_room(self) -> None:
        lines = PLUGIN_MODULE.build_limits_block(
            limits={"max_position_ratio": 0.36, "min_cash_reserve_ratio": 0.02, "max_trades_per_day": 12},
            balance={"totalAssets": 500000},
            positions=[self._holding("600519", "贵州茅台", 160000), self._holding("300750", "宁德时代", 46500)],
            trades_used=3,
        )
        text = "\n".join(lines)
        self.assertIn("单票上限 36%", text)
        self.assertIn("贵州茅台(600519) 占 32.0%", text)
        self.assertIn("还能再加", text)
        self.assertIn("今天已下 3 笔，上限 12 笔，还能下 9 笔", text)

    def test_empty_position_still_reports_caps(self) -> None:
        lines = PLUGIN_MODULE.build_limits_block(
            limits={"max_position_ratio": 0.2, "min_cash_reserve_ratio": 0.2, "max_trades_per_day": 6},
            balance={"totalAssets": 100000},
            positions=[],
            trades_used=0,
        )
        text = "\n".join(lines)
        self.assertIn("现在空仓", text)
        self.assertIn("总仓位上限 80%", text)
        self.assertIn("还能下 6 笔", text)

    def test_trades_left_never_negative(self) -> None:
        lines = PLUGIN_MODULE.build_limits_block(
            limits={"max_position_ratio": 0.2, "min_cash_reserve_ratio": 0.2, "max_trades_per_day": 6},
            balance={"totalAssets": 100000},
            positions=[],
            trades_used=9,
        )
        self.assertIn("还能下 0 笔", "\n".join(lines))

    def test_holdings_without_assets_are_not_reported_empty(self) -> None:
        """有持仓但总资产取不到时，不能说自己空仓。"""

        lines = PLUGIN_MODULE.build_limits_block(
            limits={"max_position_ratio": 0.2, "min_cash_reserve_ratio": 0.2, "max_trades_per_day": 6},
            balance={},
            positions=[self._holding("600519", "贵州茅台", 190000)],
            trades_used=0,
        )
        text = "\n".join(lines)
        self.assertNotIn("空仓", text)
        self.assertIn("总资产取不到", text)

    def test_holdings_with_zero_assets_are_not_reported_empty(self) -> None:
        lines = PLUGIN_MODULE.build_limits_block(
            limits={"max_position_ratio": 0.2, "min_cash_reserve_ratio": 0.2, "max_trades_per_day": 6},
            balance={"totalAssets": 0},
            positions=[self._holding("600519", "贵州茅台", 190000)],
            trades_used=0,
        )
        self.assertNotIn("空仓", "\n".join(lines))


class ConfigUiSchemaTests(unittest.TestCase):
    """WebUI 配置页：中文标签、页签、不丢字段。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = PLUGIN_MODULE.SimTraderPlugin.build_config_schema(
            plugin_id="github.42yoru.maimai-sim-trader",
            plugin_name="麦麦模拟盘",
            plugin_version="0.7.0",
            plugin_description="",
            plugin_author="42yoru",
        )

    def test_layout_is_tabs(self) -> None:
        layout = self.raw["layout"]
        self.assertEqual(layout["type"], "tabs")
        tab_titles = [tab["title"] for tab in layout["tabs"]]
        self.assertEqual(tab_titles, ["常用设置", "交易", "模型", "高级"])

    def test_no_config_field_is_dropped(self) -> None:
        """每个真实配置节里的每个字段，都必须出现在重组后的 schema 里。"""

        dumped = PLUGIN_MODULE.SimTraderConfig().model_dump()
        expected = {section: set(fields) for section, fields in dumped.items()}

        covered = {}
        for block in self.raw["sections"].values():
            covered.setdefault(block["name"], set()).update(block["fields"])

        self.assertEqual(set(covered), set(expected))
        for section_name, fields in expected.items():
            self.assertEqual(covered[section_name], fields, f"{section_name} 的字段被漏掉了")

    def test_labels_are_chinese(self) -> None:
        for block in self.raw["sections"].values():
            for field_name, field in block["fields"].items():
                self.assertNotEqual(field["label"], field_name, f"{field_name} 没有中文标签")

    def test_secret_and_readonly_and_choices(self) -> None:
        mx = self.raw["sections"]["mx"]["fields"]
        self.assertEqual(mx["api_key"]["ui_type"], "password")

        plugin_block = self.raw["sections"]["plugin_info"]["fields"]
        self.assertTrue(plugin_block["config_version"]["disabled"])

        risk = self.raw["sections"]["risk"]["fields"]
        self.assertEqual(risk["profile"]["choices"], ["steady", "aggressive", "yolo"])


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

    def test_add_rejects_non_six_digit_code(self) -> None:
        """池子里混进错码会让每轮取行情都抛错，所以加的时候就要挡住。"""

        with tempfile.TemporaryDirectory() as tmp:
            store = WatchlistStore(Path(tmp) / "watchlist.json")
            store.load()
            for bad in ("60051", "6005190", "sh600519", "abcdef", "  "):
                with self.assertRaises(ValueError):
                    store.add(bad, "坏代码")
            self.assertTrue(store.add(" 600519 ", "贵州茅台"))


class HoldingTests(unittest.TestCase):
    """持仓字段适配：字段名一变就得认出来，认不出就报错。"""

    def test_unparseable_quantity_raises(self) -> None:
        """数量格式变了（如 "1,500"）必须报错：折成 0 会让单票仓位上限静默失效。"""

        with self.assertRaises(ValueError):
            parse_holdings([{"stockCode": "600036", "quantity": "1,500", "availableQty": 500}])

    def test_missing_quantity_is_zero(self) -> None:
        """字段完全缺失仍按 0 处理（妙想少给字段不等于格式变了）。"""

        holdings = parse_holdings([{"stockCode": "600036", "availableQty": 500}])

        self.assertEqual(holdings["600036"].quantity, 0)

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

    def test_mx_fixed_point_shape(self) -> None:
        """妙想真实返回：定点整数 + 小数位，必须缩放成真实价格。"""

        holdings = parse_holdings(
            [
                {
                    "secCode": "000333",
                    "secMkt": 0,
                    "count": 1500,
                    "availCount": 0,
                    "value": 127980.0,
                    "costPriceDec": 3,
                    "costPrice": 85236,
                    "priceDec": 2,
                    "price": 8532,
                    "profit": 125.998,
                    "secName": "美的集团",
                    "posPct": 12.8,
                }
            ]
        )
        holding = holdings["000333"]
        self.assertEqual(holding.name, "美的集团")
        self.assertEqual(holding.quantity, 1500)
        self.assertEqual(holding.available, 0)
        self.assertAlmostEqual(holding.avg_cost, 85.236, places=3)
        self.assertAlmostEqual(holding.current_price, 85.32, places=2)
        self.assertAlmostEqual(holding.market_value, 127980.0)
        self.assertAlmostEqual(holding.profit, 125.998, places=3)


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
            state.record_round(
                record=STATE.RoundRecord(
                    at="10-19 10:00",
                    mood="平静",
                    reasoning="等着",
                    actions=["想买 600519，但资金不够，没买"],
                ),
                trade_count=0,
            )
            state.next_check_at = 123.0
            store.save()
            reloaded = StateStore(path).load()
            self.assertEqual(reloaded.check_count, 1)
            self.assertEqual(reloaded.next_check_at, 123.0)
            self.assertEqual(len(reloaded.round_records), 1)
            record = reloaded.round_records[0]
            self.assertEqual(record.at, "10-19 10:00")
            self.assertEqual(record.mood, "平静")
            self.assertEqual(record.reasoning, "等着")
            self.assertEqual(record.actions, ["想买 600519，但资金不够，没买"])

    def test_legacy_recent_notes_are_migrated(self) -> None:
        """老状态文件里的 recent_notes 升级后不能丢。"""

        legacy = {
            "version": STATE.STATE_VERSION,
            "trade_date": "2026-10-19",
            "recent_notes": ["10:00 没动", "10:30 动了 1 笔"],
        }
        state = RuntimeState.from_dict(legacy)
        self.assertEqual(
            [record.actions for record in state.round_records],
            [["10:00 没动"], ["10:30 动了 1 笔"]],
        )
        self.assertEqual([record.reasoning for record in state.round_records], ["", ""])

    def test_round_records_are_capped(self) -> None:
        state = RuntimeState()
        for index in range(STATE.MAX_ROUND_RECORDS + 5):
            state.record_round(
                record=STATE.RoundRecord(at=f"10-19 10:{index:02d}", mood="平静", reasoning=f"第 {index} 轮"),
                trade_count=0,
            )
        self.assertEqual(len(state.round_records), STATE.MAX_ROUND_RECORDS)
        self.assertEqual(state.round_records[-1].reasoning, f"第 {STATE.MAX_ROUND_RECORDS + 4} 轮")

    def test_reasoning_survives_day_rollover(self) -> None:
        """跨日只清当日计数，历史记录得留着，不然「过去」就断了。"""

        state = RuntimeState(trade_date="2026-10-18")
        state.record_round(
            record=STATE.RoundRecord(at="10-18 10:00", mood="谨慎", reasoning="等回调"),
            trade_count=0,
        )
        state.roll_date("2026-10-19")
        self.assertEqual(state.last_reasoning, "")
        self.assertEqual(len(state.round_records), 1)
        self.assertEqual(state.round_records[0].reasoning, "等回调")

    def test_round_record_rejects_bad_actions(self) -> None:
        with self.assertRaises(STATE.StateError):
            STATE.RoundRecord.from_dict({"at": "10-19 10:00", "actions": "不是数组"})

    def test_round_record_rejects_non_dict(self) -> None:
        with self.assertRaises(STATE.StateError):
            STATE.RoundRecord.from_dict("oops")

    def test_loaded_records_are_capped(self) -> None:
        """手改过的状态文件也不能超过上限。"""

        raw = {
            "version": STATE.STATE_VERSION,
            "round_records": [
                {"at": f"10-19 10:{index:02d}", "reasoning": f"第 {index} 轮"} for index in range(STATE.MAX_ROUND_RECORDS + 6)
            ],
        }
        state = RuntimeState.from_dict(raw)
        self.assertEqual(len(state.round_records), STATE.MAX_ROUND_RECORDS)
        self.assertEqual(state.round_records[-1].reasoning, f"第 {STATE.MAX_ROUND_RECORDS + 5} 轮")

    def test_prompt_block_without_time_still_renders(self) -> None:
        """老数据没有时间戳，也不能渲染出个空字符串。"""

        block = STATE.RoundRecord(reasoning="10:00 没动").to_prompt_block()
        self.assertIn("心情：平静", block)
        self.assertIn("判断：10:00 没动", block)
        self.assertIn("动作：没动", block)

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


def _snapshot_line(code: str, *, price: float = 10.0, prev_close: float = 9.9) -> str:
    """构造一行合法的腾讯快照（40 个字段，下标与 quotes.py 一致）。"""

    fields = ["1", "测试票", code, f"{price}", f"{prev_close}", "9.95", "1234"]
    while len(fields) < 30:
        fields.append("0")
    fields.append("20261028103000")  # 30 时间
    fields.append("0.1")  # 31 涨跌
    fields.append("1.01")  # 32 涨跌幅
    fields.append("10.5")  # 33 最高
    fields.append("9.8")  # 34 最低
    while len(fields) < 37:
        fields.append("0")
    fields.append("1234")  # 37 成交额
    fields.append("2.5")  # 38 换手率
    fields.append("15.0")  # 39 市盈率
    return f'v_sz{code}="{"~".join(fields)}";'


class QuoteParseTests(unittest.TestCase):
    """一只坏票不该拖垮整个股票池，但全坏了必须报错。"""

    def test_none_match_symbol_does_not_break_batch(self) -> None:
        """腾讯对查不到的代码回 v_pv_none_match，不能当成整批失败。"""

        quotes = parse_snapshot(_snapshot_line("600036") + ';v_pv_none_match="1";')

        self.assertEqual(list(quotes), ["600036"])

    def test_one_broken_symbol_does_not_break_batch(self) -> None:
        quotes = parse_snapshot(_snapshot_line("600036") + 'v_sz000001="1~2~3";')

        self.assertEqual(list(quotes), ["600036"])

    def test_all_broken_raises(self) -> None:
        with self.assertRaises(QUOTES.QuoteError):
            parse_snapshot('v_sz000001="1~2~3";')

    def test_suspended_flag_when_price_is_zero(self) -> None:
        quotes = parse_snapshot(_snapshot_line("600036", price=0.0))

        self.assertTrue(quotes["600036"].suspended)
        # 现价 0 时用昨收顶上，避免后续除零，但这条不能拿去下单
        self.assertAlmostEqual(quotes["600036"].price, 9.9)
