"""整轮流程集成测试：用假 ctx / 假妙想 / 假行情跑通 _run_round。

覆盖的是编排层：拉数据 → 决策 → 下单前校验 → 播报 → 状态落盘。
真正的网络和模型调用不在这里验（那部分靠真机）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import importlib.util
import httpx
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from maibot_sdk import PluginContext, PluginPaths

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

PLUGIN_MODULE = sys.modules[MODULE_NAME]
QUOTES = sys.modules[f"{MODULE_NAME}.quotes"]
STATE = sys.modules[f"{MODULE_NAME}.state"]
WATCH = sys.modules[f"{MODULE_NAME}.watchlist"]

GROUP_ID = "123456789"
GROUP_SESSION = "group-session-1"

DECISION_TEXT = json.dumps(
    {
        "mood": "手有点痒",
        "reasoning": "空仓太久，先拿三成试水",
        "actions": [
            {"action": "buy", "code": "601899", "name": "紫金矿业", "quantity": 3400, "reason": "放量走强"},
            {"action": "buy", "code": "600309", "name": "万华化学", "quantity": 1500, "reason": "领涨"},
        ],
        "pool_change": {"add": [{"code": "601988", "name": "中国银行"}], "remove": ["600660"]},
        "next_check_minutes": 60,
        "speech": "先伸一只脚试试水",
    },
    ensure_ascii=False,
)


class FakeMx:
    """假的妙想客户端，记录下单。"""

    def __init__(self) -> None:
        self.placed: List[Dict[str, Any]] = []

    async def get_balance(self) -> Dict[str, Any]:
        return {
            "totalAssets": 1000000.0,
            "availBalance": 1000000.0,
            "totalPosValue": 0.0,
            "initMoney": 1000000.0,
            "nav": 1.0,
            "totalPosPct": 0.0,
        }

    async def get_positions(self) -> List[Dict[str, Any]]:
        return []

    async def get_orders(self) -> List[Dict[str, Any]]:
        return []

    async def place_order(self, *, direction: str, stock_code: str, price: float, quantity: int) -> Dict[str, Any]:
        self.placed.append({"direction": direction, "code": stock_code, "price": price, "quantity": quantity})
        return {"orderId": f"fake-{len(self.placed)}", "status": "accepted"}

    async def close(self) -> None:
        return None


class FakeQuotes:
    """假行情：价格跟真实值差不多。"""

    PRICES = {"601899": 29.92, "600309": 66.24, "600660": 54.78, "601988": 6.92}

    async def fetch(self, codes: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for code in codes:
            if code not in self.PRICES:
                continue
            price = self.PRICES[code]
            result[code] = QUOTES.Quote(
                code=code,
                name=code,
                price=price,
                prev_close=price,
                open_price=price,
                high=price,
                low=price,
                change=0.0,
                change_pct=0.0,
                volume_lots=1000.0,
                turnover_wan=1000.0,
                turnover_rate=1.0,
                pe=10.0,
                updated_at="20261019100000",
            )
        return result

    async def close(self) -> None:
        return None


class FakeHarness:
    """假的宿主：拦截 llm.generate / send.text / chat.*。"""

    def __init__(self) -> None:
        self.sent: List[Dict[str, str]] = []
        self.llm_prompts: List[Any] = []
        self.llm_args: List[Dict[str, Any]] = []

    async def rpc(self, method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
        del method, plugin_id, timeout_ms
        capability = payload["capability"]
        args = payload["args"]
        if capability == "llm.generate":
            self.llm_prompts.append(args.get("prompt"))
            self.llm_args.append(dict(args))
            return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
        if capability == "send.text":
            self.sent.append({"stream_id": args["stream_id"], "text": args["text"]})
            return {"success": True, "sent": True, "message_id": "m1"}
        if capability == "chat.get_group_streams":
            return [{"session_id": GROUP_SESSION, "group_id": GROUP_ID, "group_name": "熟人社会", "account_id": "1000"}]
        if capability == "chat.get_private_streams":
            return []
        raise AssertionError(f"未预期的能力调用: {capability}")


def _quotes_with_suspended(code: str) -> Any:
    """假行情，但把某只票标成停牌（现价 0，price 用昨收顶上）。"""

    stamp = f"{datetime.now().strftime('%Y%m%d')}100000"
    base = FakeQuotes()

    class SuspendedQuotes:
        async def fetch(self, codes: Any) -> Dict[str, Any]:
            snapshot = await base.fetch(codes)
            result: Dict[str, Any] = {}
            for item_code, quote in snapshot.items():
                patched = replace(quote, updated_at=stamp)
                if item_code == code:
                    patched = replace(patched, suspended=True)
                result[item_code] = patched
            return result

        async def close(self) -> None:
            return None

    return SuspendedQuotes()


def _quotes_stamped_today() -> Any:
    """假行情，但把快照日期改成今天。

    ``_run_round`` 会把「行情最新日期 != 今天」当成非交易日直接返回，
    所以跑真实 ``now()`` 的用例必须让快照日期跟着走，否则这条测试会随日期腐烂。
    """

    stamp = f"{datetime.now().strftime('%Y%m%d')}100000"
    base = FakeQuotes()

    class TodayQuotes:
        async def fetch(self, codes: Any) -> Dict[str, Any]:
            snapshot = await base.fetch(codes)
            return {code: replace(quote, updated_at=stamp) for code, quote in snapshot.items()}

        async def close(self) -> None:
            return None

    return TodayQuotes()


def build_plugin(tmp_dir: Path, *, overrides: Optional[Dict[str, Any]] = None) -> Any:
    """构造一个装好假依赖的插件实例；overrides 按配置节覆盖默认值。"""

    plugin = PLUGIN_MODULE.SimTraderPlugin()
    config = PLUGIN_MODULE.SimTraderPlugin.build_default_config()
    if overrides:
        for section, values in overrides.items():
            config[section].update(values)
    config["mx"]["api_key"] = "test-key"
    config["notify"]["group_ids"] = [GROUP_ID]
    config["owner"]["owners"] = [f"qq:{OWNER_USER}"]
    plugin.set_plugin_config(config)

    paths = PluginPaths(data_dir=tmp_dir / "data", runtime_dir=tmp_dir / "runtime")
    ctx = PluginContext(PLUGIN_MODULE.SimTraderPlugin.__name__, rpc_call=FakeHarness().rpc, paths=paths)
    plugin._set_context(ctx)

    plugin._watchlist = WATCH.WatchlistStore(paths.data_dir / "watchlist.json")
    plugin._state = STATE.StateStore(paths.data_dir / "state.json")
    plugin._watchlist.load()
    plugin._state.load()
    plugin._mx = FakeMx()
    plugin._quotes = FakeQuotes()
    return plugin


class RoundFlowTests(unittest.IsolatedAsyncioTestCase):
    """整轮编排。"""

    async def test_dry_run_never_places_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            notes = await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", dry_run=True)
            self.assertEqual(len(plugin._mx.placed), 0)
            buy_notes = [note for note in notes if note.startswith("（试跑）会买")]
            self.assertEqual(len(buy_notes), 2)

    async def test_zero_max_tokens_follows_host_task(self) -> None:
        """max_tokens=0 表示跟随宿主任务，不能往请求里塞一个死数字。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", dry_run=True)

            self.assertTrue(harness.llm_args)
            self.assertNotIn("max_tokens", harness.llm_args[0])

    async def test_explicit_max_tokens_is_forwarded(self) -> None:
        """填了正数就照传，用户想自己限长时仍然有效。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.model.max_tokens = 6000
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", dry_run=True)

            self.assertEqual(harness.llm_args[0]["max_tokens"], 6000)

    async def test_suspended_stock_is_not_traded(self) -> None:
        """停牌票的 price 是昨收顶替的，不能拿它算限价去下单。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            plugin._quotes = _quotes_with_suspended("601899")

            notes = await plugin._run_round(
                datetime.now(), datetime.now().strftime("%Y-%m-%d"), announce=False
            )

            placed_codes = [item["code"] for item in plugin._mx.placed]
            self.assertNotIn("601899", placed_codes)
            self.assertTrue(any("停牌" in line for line in notes), notes)

    async def test_snapshot_uses_account_after_trades(self) -> None:
        """播报用的是成交后重取的账户，不是下单前那份快照。

        否则空仓建仓那一轮会在群里写「持仓市值 0.00 元 / 当前空仓」，正文却说刚买了三成仓。
        """

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()

            class CountingMx(FakeMx):
                """第一次查账户是空仓，下过单之后返回成交后的账户。"""

                def __init__(self) -> None:
                    super().__init__()
                    self.balance_calls = 0

                async def get_balance(self) -> Dict[str, Any]:
                    self.balance_calls += 1
                    data = await super().get_balance()
                    if self.placed:
                        return {**data, "availBalance": 900000.0, "totalPosValue": 100000.0, "totalPosPct": 10.0}
                    return data

            plugin._mx = CountingMx()

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "llm.generate" and len(harness.llm_prompts) >= 1:
                    harness.llm_prompts.append(payload["args"].get("prompt"))
                    return {"success": True, "response": "先伸一只脚试试水～", "reasoning": "", "model": "fake"}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc

            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")

            self.assertEqual(len(plugin._mx.placed), 2)
            # 轮首一次 + 成交后重取一次
            self.assertGreaterEqual(plugin._mx.balance_calls, 2)
            prompt_text = "\n".join(str(item.get("content", "")) for item in harness.llm_prompts[-1])
            # 播报 prompt 里的账户快照必须是成交后的：可用资金 90 万、仓位 10%
            self.assertIn("900,000", prompt_text)
            self.assertIn("10.00%", prompt_text)

    async def test_live_round_places_orders_and_broadcasts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "llm.generate" and len(harness.llm_prompts) >= 1:
                    # 第二次 llm 调用是播报文案，返回一句人话
                    harness.llm_prompts.append(payload["args"].get("prompt"))
                    return {"success": True, "response": "先伸一只脚试试水～", "reasoning": "", "model": "fake"}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc

            notes = await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")
            self.assertEqual(len(plugin._mx.placed), 2)
            self.assertEqual(plugin._mx.placed[0]["code"], "601899")
            self.assertEqual(plugin._mx.placed[0]["quantity"], 3400)
            self.assertEqual(len(harness.sent), 1)
            self.assertEqual(harness.sent[0]["stream_id"], GROUP_SESSION)
            self.assertIn("试试水", harness.sent[0]["text"])
            self.assertTrue(notes)

    async def test_dry_run_keeps_pool_untouched(self) -> None:
        """试跑不留副作用：池子不该被改。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            notes = await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", dry_run=True)
            codes = {item.code for item in plugin._watchlist.all()}
            self.assertIn("600660", codes)
            self.assertNotIn("601988", codes)
            self.assertTrue(any("会加入" in note for note in notes))

    async def test_live_round_applies_pool_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")
            codes = {item.code for item in plugin._watchlist.all()}
            self.assertIn("601988", codes)
            self.assertNotIn("600660", codes)

    async def test_state_updated_after_live_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin._state.load().roll_date("2026-10-19")
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")
            state = plugin._state.load()
            self.assertEqual(state.check_count, 1)
            self.assertEqual(state.trade_count, 2)
            self.assertEqual(state.last_mood, "手有点痒")
            self.assertGreater(state.next_check_at, 0.0)
            self.assertEqual(state.trade_date, "2026-10-19")

    async def test_round_record_is_persisted(self) -> None:
        """整轮跑完要留下「当时怎么想、做了什么」，下一轮才能接上。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin._state.load().roll_date("2026-10-19")
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")
            records = plugin._state.load().round_records
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record.at, "10-19 10:00")
            self.assertEqual(record.mood, "手有点痒")
            self.assertEqual(record.reasoning, "空仓太久，先拿三成试水")
            # 成交行必须留下，别被池子变更挤掉
            self.assertTrue(any(line.startswith("买了") for line in record.actions), record.actions)

    async def test_chat_triggered_round_rolls_date(self) -> None:
        """只靠聊天触发（schedule 关掉）时也要跨日重置，否则笔数会一直累加。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            state = plugin._state.load()
            state.trade_date = "2026-10-18"
            state.trade_count = 5
            plugin._state.save()
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", announce=False)
            state = plugin._state.load()
            self.assertEqual(state.trade_date, "2026-10-19")
            self.assertEqual(state.trade_count, 2)

    async def test_missing_group_stream_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "chat.get_group_streams":
                    return []
                if payload["capability"] == "llm.generate":
                    return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
                raise AssertionError(payload["capability"])

            plugin.ctx._rpc_call = rpc
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19")
            # 找不到群只是跳过播报，不影响下单
            self.assertEqual(len(plugin._mx.placed), 2)


class TickTests(unittest.IsolatedAsyncioTestCase):
    """_tick 的时间判断：什么时段该干活、什么时段该闭嘴。"""

    def _patch_now(self, moment: datetime) -> Any:
        fake = mock.MagicMock()
        fake.now.return_value = moment
        return mock.patch.object(PLUGIN_MODULE, "datetime", fake)

    async def test_runs_inside_trading_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            with self._patch_now(datetime(2026, 10, 19, 10, 0)):
                await plugin._tick()
            self.assertEqual(len(plugin._mx.placed), 2)

    async def test_skips_lunch_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            with self._patch_now(datetime(2026, 10, 19, 12, 0)):
                await plugin._tick()
            self.assertEqual(len(plugin._mx.placed), 0)

    async def test_skips_weekend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            with self._patch_now(datetime(2026, 10, 17, 10, 0)):
                await plugin._tick()
            self.assertEqual(len(plugin._mx.placed), 0)

    async def test_respects_next_check_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            state = plugin._state.load()
            state.next_check_at = 9e18
            plugin._state.save()
            with self._patch_now(datetime(2026, 10, 19, 10, 0)):
                await plugin._tick()
            self.assertEqual(len(plugin._mx.placed), 0)

    async def test_failure_pushes_next_check_forward(self) -> None:
        """决策失败时必须把下次时间推后，否则会一分钟一轮地烧模型。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))

            async def broken_rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "llm.generate":
                    return {"success": False, "error": "boom"}
                raise AssertionError(payload["capability"])

            plugin.ctx._rpc_call = broken_rpc
            with self._patch_now(datetime(2026, 10, 19, 10, 0)):
                with self.assertRaises(RuntimeError):
                    await plugin._tick()
            state = plugin._state.load()
            self.assertGreater(state.next_check_at, 0.0)


    async def test_dry_run_uses_host_bot_name(self) -> None:
        """试跑提示语要用宿主昵称，不能写死。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin._bot_name = "小爱"
            sent: List[str] = []

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                capability = payload["capability"]
                if capability == "send.text":
                    sent.append(payload["args"]["text"])
                    return {"success": True, "sent": True, "message_id": "m1"}
                if capability == "llm.generate":
                    return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
                if capability == "chat.get_group_streams":
                    return []
                raise AssertionError(capability)

            plugin.ctx._rpc_call = rpc
            await plugin._handle_dry_run("stream-x")
            self.assertTrue(sent)
            self.assertIn("小爱", sent[0])
            self.assertNotIn("麦麦", sent[0])


OWNER_USER = "100000001"
STRANGER_USER = "999999999"


class CommandTests(unittest.IsolatedAsyncioTestCase):
    """命令分派、主人校验、宽松行情查询。"""

    async def _dispatch(self, plugin: Any, rest: str, *, user_id: str) -> List[str]:
        sent: List[str] = []

        async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
            capability = payload["capability"]
            if capability == "send.text":
                sent.append(payload["args"]["text"])
                return {"success": True, "sent": True, "message_id": "m1"}
            if capability == "llm.generate":
                return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
            raise AssertionError(capability)

        plugin.ctx._rpc_call = rpc
        await plugin.handle_stock(
            stream_id="stream-1",
            platform="qq",
            user_id=user_id,
            matched_groups={"rest": rest},
        )
        return sent

    async def test_stranger_cannot_edit_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            sent = await self._dispatch(plugin, "加 600519 贵州茅台", user_id=STRANGER_USER)
            self.assertTrue(any("只有主人" in text for text in sent))
            self.assertNotIn("600519", {item.code for item in plugin._watchlist.all()})

    async def test_owner_can_edit_pool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            sent = await self._dispatch(plugin, "加 600519 贵州茅台", user_id=OWNER_USER)
            self.assertIn("600519", {item.code for item in plugin._watchlist.all()})
            self.assertTrue(any("加进池子" in text for text in sent))

    async def test_rate_command_updates_next_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            before = plugin._state.load().next_check_at
            sent = await self._dispatch(plugin, "节奏 15", user_id=OWNER_USER)
            after = plugin._state.load().next_check_at
            self.assertGreater(after, before)
            self.assertTrue(any("15 分钟" in text for text in sent))

    async def test_stranger_cannot_change_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            sent = await self._dispatch(plugin, "节奏 15", user_id=STRANGER_USER)
            self.assertTrue(any("只有主人" in text for text in sent))

    async def test_quote_by_code_and_name(self) -> None:
        """三种写法都应该查到同一只票。"""

        for rest in ("行情 601899", "紫金矿业", "601899 紫金矿业", "紫金矿业 601899"):
            with tempfile.TemporaryDirectory() as tmp:
                plugin = build_plugin(Path(tmp))
                sent = await self._dispatch(plugin, rest, user_id=STRANGER_USER)
                self.assertTrue(sent, f"{rest} 没有回复")
                self.assertIn("601899", sent[0], f"{rest} 没查到 601899")

    async def test_unknown_query_gives_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            sent = await self._dispatch(plugin, "不存在的股票", user_id=STRANGER_USER)
            self.assertTrue(any("没认出" in text for text in sent))

    async def test_history_and_orders_respond(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            for rest in ("记录", "委托"):
                sent = await self._dispatch(plugin, rest, user_id=STRANGER_USER)
                self.assertTrue(sent, f"{rest} 没有回复")


class ToolTests(unittest.IsolatedAsyncioTestCase):
    """自然语言工具。"""

    async def test_query_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            for action in ("status", "pool", "orders", "history"):
                result = await plugin.tool_query(action=action)
                self.assertTrue(result["success"], f"{action} 失败: {result}")

    async def test_query_quote_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            result = await plugin.tool_query(action="quote", query="紫金矿业")
            self.assertTrue(result["success"])
            self.assertIn("601899", result["content"])

    async def test_query_quote_needs_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            result = await plugin.tool_query(action="quote")
            self.assertFalse(result["success"])

    async def test_query_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            result = await plugin.tool_query(action="whatever")
            self.assertFalse(result["success"])
            self.assertIn("status", result["content"])

    async def test_review_places_orders_without_broadcast(self) -> None:
        """聊天里触发的看盘会真下单，但不额外往群里播报。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            # 闭市时 review 会自动降级为试跑，这条要验"真下单"，所以把时段设成现在
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            sent: List[str] = []

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                capability = payload["capability"]
                if capability == "send.text":
                    sent.append(payload["args"]["text"])
                    return {"success": True, "sent": True, "message_id": "m1"}
                if capability == "llm.generate":
                    return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
                raise AssertionError(capability)

            plugin.ctx._rpc_call = rpc
            # 这条走的是真实 now()，假行情的日期要换成今天，否则会被判成非交易日
            plugin._quotes = _quotes_stamped_today()
            result = await plugin.tool_review(reason="群里有人怂恿加仓")
            self.assertTrue(result["success"], result)
            self.assertEqual(len(plugin._mx.placed), 2)
            self.assertEqual(sent, [], "工具触发的看盘不该再发群播报")
            self.assertIn("心情", result["content"])


class StartupConfigTests(unittest.IsolatedAsyncioTestCase):
    """首次下载后的默认配置与启动行为：不带作者个人信息，也不空转。"""

    def test_default_config_carries_no_personal_ids(self) -> None:
        """默认配置里不该预置任何群号 / 用户 ID（否则别人装上会往作者群里发）。"""

        config = PLUGIN_MODULE.SimTraderPlugin.build_default_config()

        self.assertEqual(config["notify"]["group_ids"], [])
        self.assertEqual(config["notify"]["private_user_ids"], [])
        self.assertEqual(config["owner"]["owners"], [])
        self.assertEqual(config["mx"]["api_key"], "")

    async def test_loop_not_started_without_api_key(self) -> None:
        """没有 API Key 就不起后台循环，免得交易时段每轮抛一次异常。"""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MX_APIKEY": ""}):
            plugin = build_plugin(Path(tmp))
            plugin.config.mx.api_key = ""

            await plugin._restart_loop()

            self.assertIsNone(plugin._task)

    async def test_loop_started_with_api_key(self) -> None:
        """有 Key 就正常起循环。"""

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MX_APIKEY": ""}):
            plugin = build_plugin(Path(tmp))

            await plugin._restart_loop()

            self.assertIsNotNone(plugin._task)
            await plugin._stop_loop()
            self.assertIsNone(plugin._task)

    async def test_non_https_base_url_is_rejected(self) -> None:
        """base_url 会带着 API Key 一起发请求，只允许 https。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin._mx = None
            plugin.config.mx.base_url = "http://127.0.0.1:8000"

            with self.assertRaises(RuntimeError) as raised:
                plugin._require_mx()

            self.assertIn("https", str(raised.exception))

    async def test_fetch_skips_invalid_pool_codes(self) -> None:
        """池子里有坏代码时只跳过它，不让整轮行情拉挂掉。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin._watchlist._items = [
                WATCH.PoolItem(code="601899", name="紫金矿业", added_at=0.0, note=""),
                WATCH.PoolItem(code="坏代码", name="坏代码", added_at=0.0, note=""),
            ]

            quotes = await plugin._fetch_pool_quotes()

            self.assertEqual(sorted(quotes), ["601899"])

    async def test_missing_notify_and_owner_are_reported(self) -> None:
        """播报目标和主人白名单为空时，启动日志里要有明确提示。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.notify.group_ids = []
            plugin.config.owner.owners = []

            with self.assertLogs(plugin.ctx.logger, level="WARNING") as captured:
                plugin._warn_if_not_configured()

            joined = "\n".join(captured.output)
            self.assertIn("没有配置播报目标", joined)
            self.assertIn("owner.owners 为空", joined)


def _session_covering(now: datetime) -> List[str]:
    """给一个包含当前时刻的交易时段。"""

    start = (now - timedelta(minutes=5)).strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    return [f"{start}-{end}"]


def _session_far_from(now: datetime) -> List[str]:
    """给一个肯定不含当前时刻的交易时段。"""

    return ["00:00-00:01"] if now.hour >= 12 else ["23:00-23:01"]


class ManualTriggerGateTests(unittest.IsolatedAsyncioTestCase):
    """手动看盘的闸门：闭市降级为试跑、同一聊天流有冷却。"""

    async def test_review_outside_session_does_not_place_orders(self) -> None:
        """闭市时聊天里触发看盘：可以跑决策，但不能真下单。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            now = datetime.now()
            plugin.config.schedule.sessions = _session_far_from(now)
            plugin._quotes = _quotes_stamped_today()

            result = await plugin.tool_review(reason="群里有人问")

            self.assertTrue(result["success"], result)
            self.assertIn("不在交易时段", result["content"])
            self.assertEqual(len(plugin._mx.placed), 0)

    async def test_review_inside_session_places_orders(self) -> None:
        """交易时段内触发看盘：照常下单。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            plugin._quotes = _quotes_stamped_today()

            result = await plugin.tool_review(reason="开盘了")

            self.assertTrue(result["success"], result)
            self.assertNotIn("不在交易时段", result["content"])
            self.assertEqual(len(plugin._mx.placed), 2)

    async def test_review_respects_cooldown(self) -> None:
        """同一聊天流连着触发：第二次被冷却挡下。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            plugin._quotes = _quotes_stamped_today()

            first = await plugin.tool_review(stream_id=GROUP_SESSION)
            second = await plugin.tool_review(stream_id=GROUP_SESSION)

            self.assertTrue(first["success"], first)
            self.assertFalse(second["success"], second)
            self.assertIn("秒后再来", second["content"])

    async def test_dry_run_respects_cooldown(self) -> None:
        """`/股 试跑` 与工具共用冷却，反复触发只会烧一次 token。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            await plugin._handle_dry_run(GROUP_SESSION)
            await plugin._handle_dry_run(GROUP_SESSION)

            texts = [item["text"] for item in harness.sent]
            self.assertTrue(any("试跑结果" in text for text in texts), texts)
            self.assertTrue(any("秒后再试" in text for text in texts), texts)
            self.assertEqual(len(harness.llm_prompts), 1)

    async def test_dry_run_refused_while_another_round_runs(self) -> None:
        """后台循环正在跑的时候，试跑不该再起一轮。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc
            await plugin._round_lock.acquire()
            try:
                await plugin._handle_dry_run(GROUP_SESSION)
            finally:
                plugin._round_lock.release()

            self.assertEqual(harness.llm_prompts, [])
            self.assertTrue(any("已经有一轮在看盘" in item["text"] for item in harness.sent), harness.sent)

    async def test_round_refuses_when_lock_held(self) -> None:
        """另一轮占着锁时，第二轮直接拒绝——布尔标志在「读-置」之间隔着 await，会放过并发。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            plugin._quotes = _quotes_stamped_today()
            await plugin._round_lock.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    await plugin._run_round(datetime.now(), datetime.now().strftime("%Y-%m-%d"), dry_run=True)
            finally:
                plugin._round_lock.release()

    async def test_round_releases_lock_afterwards(self) -> None:
        """一轮跑完（无论成败）都要释放锁，否则后台循环会永久停摆。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.schedule.sessions = _session_covering(datetime.now())
            plugin._quotes = _quotes_stamped_today()

            await plugin._run_round(datetime.now(), datetime.now().strftime("%Y-%m-%d"), dry_run=True)

            self.assertFalse(plugin._round_lock.locked())


class RiskProfileTests(unittest.IsolatedAsyncioTestCase):
    """风险档：倍数叠加、绝对上限夹取、状态回执。"""

    async def test_default_profile_keeps_original_numbers(self) -> None:
        """默认档（稳健）必须等于原来的行为，老用户升级后不该变。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))

            limits = plugin._effective_limits()

            self.assertEqual(plugin.config.risk.profile, "steady")
            self.assertAlmostEqual(limits["max_position_ratio"], 0.2)
            self.assertAlmostEqual(limits["min_cash_reserve_ratio"], 0.2)
            self.assertEqual(limits["max_trades_per_day"], 6)
            self.assertAlmostEqual(limits["slippage"], 0.003)

    async def test_aggressive_and_yolo_scale_up(self) -> None:
        """激进 / 梭哈 按倍数放大，且方向正确。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))

            plugin.config.risk.profile = "aggressive"
            aggressive = plugin._effective_limits()
            plugin.config.risk.profile = "yolo"
            yolo = plugin._effective_limits()

            self.assertGreater(aggressive["max_position_ratio"], 0.2)
            self.assertLess(aggressive["min_cash_reserve_ratio"], 0.2)
            self.assertGreater(yolo["max_position_ratio"], aggressive["max_position_ratio"])
            self.assertLess(yolo["min_cash_reserve_ratio"], aggressive["min_cash_reserve_ratio"])

    async def test_hard_caps_clamp_extremes(self) -> None:
        """用户把基数调到天上，档位也不能突破代码里的绝对上限。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.risk.profile = "yolo"
            plugin.config.account.max_position_ratio = 1.0
            plugin.config.account.max_trades_per_day = 30
            plugin.config.account.min_cash_reserve_ratio = 0.0
            plugin.config.account.slippage = 0.02

            limits = plugin._effective_limits()

            self.assertAlmostEqual(limits["max_position_ratio"], PLUGIN_MODULE.HARD_MAX_POSITION_RATIO)
            self.assertEqual(limits["max_trades_per_day"], PLUGIN_MODULE.HARD_MAX_TRADES_PER_DAY)
            self.assertAlmostEqual(limits["min_cash_reserve_ratio"], 0.0)
            self.assertAlmostEqual(limits["slippage"], 0.02)

    async def test_unknown_profile_raises(self) -> None:
        """写错档位名直接报错，不静默退回默认。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.risk.profile = "疯狂"

            with self.assertRaises(RuntimeError) as raised:
                plugin._effective_limits()

            self.assertIn("risk", str(raised.exception))

    async def test_status_summary_shows_profile(self) -> None:
        """状态回执里要写清当前档位与实际限制。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            plugin.config.risk.profile = "yolo"

            summary = plugin._risk_summary()

            self.assertIn("梭哈", summary)
            self.assertIn("单票≤", summary)
            self.assertIn("每日≤", summary)


class ComponentTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """长耗时组件必须显式声明 RPC 超时（宿主默认 60 秒会先掐断）。"""

    async def test_long_running_components_declare_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            entries = {item["name"]: item for item in plugin.get_components()}

            for name in ("stock", "sim_trader_review"):
                self.assertIn(name, entries)
                self.assertEqual(entries[name]["metadata"].get("timeout_ms"), 240000)


class ManualGateAndReportTests(unittest.IsolatedAsyncioTestCase):
    """手动看盘的冷却与收盘复盘的触发条件。"""

    async def test_manual_cooldown_is_global(self) -> None:
        """冷却对全局生效：换个群触发也不能绕过去连着下真实单。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp), overrides={"schedule": {"manual_cooldown_seconds": 600}})

            plugin._mark_manual_run("stream-a")

            self.assertGreater(plugin._manual_cooldown_left("stream-b"), 0)

    async def test_no_close_report_when_nothing_happened(self) -> None:
        """今天一轮都没看过盘（多半非交易日）就不发「看了 0 轮」的复盘。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc
            state = plugin._require_state().load()
            state.check_count = 0
            state.reported_dates = []

            await plugin._maybe_send_close_report(datetime(2026, 10, 19, 15, 10), "2026-10-19")

            self.assertEqual(harness.sent, [])


class TimezoneContractTests(unittest.IsolatedAsyncioTestCase):
    """交易时段与跨日必须能锁定时区（容器里默认 UTC 会整天不开盘）。"""

    async def test_now_uses_configured_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp), overrides={"schedule": {"timezone": "Asia/Shanghai"}})

            now = plugin._now()

            self.assertIsNotNone(now.tzinfo)
            self.assertEqual(getattr(now.tzinfo, "key", ""), "Asia/Shanghai")

    async def test_invalid_timezone_is_rejected_at_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp), overrides={"schedule": {"timezone": "Mars/Olympus"}})

            with self.assertRaises(RuntimeError):
                plugin._validate_timezone()


class LifecycleContractTests(unittest.IsolatedAsyncioTestCase):
    """热更新订阅：不声明的话 bot 分支是死代码。"""

    def test_declares_bot_config_subscription(self) -> None:
        subscriptions = PLUGIN_MODULE.SimTraderPlugin.config_reload_subscriptions

        self.assertIn("bot", subscriptions)
        self.assertIn("self", subscriptions)


class GuardrailTests(unittest.IsolatedAsyncioTestCase):
    """本轮新增的风控与解析护栏。"""

    async def test_same_round_repeat_buy_respects_position_cap(self) -> None:
        """同一轮里连买同一只票，也要受单票上限约束。

        轮首的 holding.market_value 不会随成交更新，第二次买入如果还按旧市值算，
        两次各 10% 就叠成了 20%+，上限形同不存在。
        """

        decision = json.dumps(
            {
                "mood": "想加仓",
                "reasoning": "同一只票分两笔买",
                "actions": [
                    {"action": "buy", "code": "601899", "name": "紫金矿业", "quantity": 3400, "reason": "第一笔"},
                    {"action": "buy", "code": "601899", "name": "紫金矿业", "quantity": 3400, "reason": "第二笔"},
                ],
                "pool_change": {"add": [], "remove": []},
                "next_check_minutes": 30,
                "speech": "",
            },
            ensure_ascii=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp), overrides={"account": {"max_position_ratio": 0.2}})
            harness = FakeHarness()

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "llm.generate":
                    harness.llm_prompts.append(payload["args"].get("prompt"))
                    return {"success": True, "response": decision, "reasoning": "", "model": "fake"}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc
            await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", announce=False)

            spent = sum(item["price"] * item["quantity"] for item in plugin._mx.placed if item["code"] == "601899")
            self.assertGreater(spent, 0)
            # 单票上限 20% × 总资产 100 万 = 20 万；允许 1% 的费用/取整误差
            self.assertLessEqual(spent, 200_000 * 1.01)

    async def test_open_buy_order_blocks_repeat_buy(self) -> None:
        """还挂着的买单（未成交）不该再对同一只票下一笔。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            original = plugin._mx.get_orders

            async def orders_with_pending() -> List[Dict[str, Any]]:
                return [
                    {"stockCode": "601899", "direction": "买入", "status": "未成交", "quantity": 3400, "price": 29.9}
                ]

            plugin._mx.get_orders = orders_with_pending  # type: ignore[method-assign]
            try:
                notes = await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", announce=False)
            finally:
                plugin._mx.get_orders = original  # type: ignore[method-assign]

            placed_codes = [item["code"] for item in plugin._mx.placed]
            self.assertNotIn("601899", placed_codes)
            self.assertTrue(any("没成交的买单" in line for line in notes), notes)

    async def test_capability_error_is_not_treated_as_no_stream(self) -> None:
        """聊天流能力报错时必须抛，不能当成「这个群没有聊天流」静默跳过播报。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"].startswith("chat."):
                    return {"success": False, "error": "能力未授权"}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc

            with self.assertRaises(TypeError):
                await plugin._resolve_group_stream(GROUP_ID)

    async def test_network_error_on_one_order_does_not_abort_round(self) -> None:
        """单笔下单网络异常不该炸穿整轮：后面的动作继续，已成交笔数不丢。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            original = plugin._mx.place_order

            async def flaky(*, direction: str, stock_code: str, price: float, quantity: int) -> Dict[str, Any]:
                if stock_code == "601899":
                    raise httpx.HTTPError("connection reset by peer")
                return await original(direction=direction, stock_code=stock_code, price=price, quantity=quantity)

            plugin._mx.place_order = flaky  # type: ignore[method-assign]
            try:
                notes = await plugin._run_round(datetime(2026, 10, 19, 10, 0), "2026-10-19", announce=False)
            finally:
                plugin._mx.place_order = original  # type: ignore[method-assign]

            placed_codes = [item["code"] for item in plugin._mx.placed]
            self.assertNotIn("601899", placed_codes)
            self.assertIn("600309", placed_codes, "一笔失败后另一笔应该照样成交")
            self.assertTrue(any("下单失败" in line for line in notes), notes)

    async def test_failed_broadcast_is_reported(self) -> None:
        """播报发送失败要留痕，不能静默丢。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "send.text":
                    return {"success": True, "sent": False}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc

            with self.assertLogs("plugin.SimTraderPlugin", level="ERROR") as captured:
                await plugin._send_to_targets("今天的播报")

            self.assertTrue(any("播报发送失败" in line for line in captured.output), captured.output)

    async def test_decision_parse_failure_retries_once(self) -> None:
        """解析失败要落原始输出并重试一次，不能整轮直接丢。"""

        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp))
            harness = FakeHarness()
            responses = ["这不是 JSON", DECISION_TEXT]

            async def rpc(method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
                if payload["capability"] == "llm.generate":
                    harness.llm_prompts.append(payload["args"].get("prompt"))
                    return {"success": True, "response": responses.pop(0), "reasoning": "", "model": "fake"}
                return await harness.rpc(method, plugin_id, payload, timeout_ms=timeout_ms)

            plugin.ctx._rpc_call = rpc
            decision = await plugin._ask_model(
                now=datetime(2026, 10, 19, 10, 0),
                balance={"totalAssets": 1000000.0, "availBalance": 1000000.0},
                positions=[],
                orders=[],
                quotes={},
            )

            self.assertGreaterEqual(len(harness.llm_prompts), 2)
            self.assertEqual(len(decision.actions), 2)
            # 第二次请求要带上纠正提示
            second = json.dumps(harness.llm_prompts[1], ensure_ascii=False)
            self.assertIn("解析失败", second)


class RiskAndPromptConfigTests(unittest.IsolatedAsyncioTestCase):
    """档位倍率与提示词都应该是用户可改的。"""

    async def test_risk_multipliers_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(
                Path(tmp),
                overrides={"risk": {"profile": "yolo", "cash_multiplier": 1.0, "trades_multiplier": 1.0}},
            )
            limits = plugin._effective_limits()

            # yolo 预设会把现金底线压到 20%×0.4=8%；覆盖成 1.0 之后应回到 20%
            self.assertAlmostEqual(limits["min_cash_reserve_ratio"], 0.2)
            self.assertEqual(limits["max_trades_per_day"], 6.0)

    async def test_prompt_template_requires_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = build_plugin(Path(tmp), overrides={"prompt": {"system_template": "只有一句话"}})

            with self.assertRaises(RuntimeError):
                plugin._validate_prompt_template()

    async def test_custom_prompt_template_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            template = (
                "{header} 自定义交易规则：只买池子里最强的。"
                "仓位上限 {max_position_pct}，现金 {min_cash_pct}。"
            )
            plugin = build_plugin(Path(tmp), overrides={"prompt": {"system_template": template}})
            harness = FakeHarness()
            plugin.ctx._rpc_call = harness.rpc

            await plugin._ask_model(
                now=datetime(2026, 10, 19, 10, 0),
                balance={"totalAssets": 1000000.0, "availBalance": 1000000.0},
                positions=[],
                orders=[],
                quotes={},
            )

            system_message = json.dumps(harness.llm_prompts[0][0], ensure_ascii=False)
            self.assertIn("自定义交易规则", system_message)


if __name__ == "__main__":
    unittest.main()
