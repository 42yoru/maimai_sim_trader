"""整轮流程集成测试：用假 ctx / 假妙想 / 假行情跑通 _run_round。

覆盖的是编排层：拉数据 → 决策 → 下单前校验 → 播报 → 状态落盘。
真正的网络和模型调用不在这里验（那部分靠真机）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import importlib.util
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

    async def rpc(self, method: str, plugin_id: str, payload: Dict[str, Any], timeout_ms: Optional[int] = None) -> Any:
        del method, plugin_id, timeout_ms
        capability = payload["capability"]
        args = payload["args"]
        if capability == "llm.generate":
            self.llm_prompts.append(args.get("prompt"))
            return {"success": True, "response": DECISION_TEXT, "reasoning": "", "model": "fake"}
        if capability == "send.text":
            self.sent.append({"stream_id": args["stream_id"], "text": args["text"]})
            return {"success": True, "sent": True, "message_id": "m1"}
        if capability == "chat.get_group_streams":
            return [{"session_id": GROUP_SESSION, "group_id": GROUP_ID, "group_name": "熟人社会", "account_id": "1000"}]
        if capability == "chat.get_private_streams":
            return []
        raise AssertionError(f"未预期的能力调用: {capability}")


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


def build_plugin(tmp_dir: Path) -> Any:
    """构造一个装好假依赖的插件实例。"""

    plugin = PLUGIN_MODULE.SimTraderPlugin()
    config = PLUGIN_MODULE.SimTraderPlugin.build_default_config()
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


if __name__ == "__main__":
    unittest.main()
