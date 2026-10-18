"""麦麦模拟盘（sim-trader）插件入口。

让麦麦用自己的判断去炒一个东财妙想模拟组合，然后在指定群/私聊里汇报盈亏。
数据分两层：妙想负责账户与深度查询，腾讯快照负责盘中报价（快、免额度）。
纯插件实现，不改主程序。
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncio
import os
import time

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool

from .decision import Decision, build_messages, parse_decision
from .mx_client import DEFAULT_BASE_URL, MxApiError, MxClient
from .positions import Holding, find_holding, parse_holdings
from .quotes import Quote, TencentQuoteClient
from .report import build_plain_fallback, build_report_messages, build_snapshot_lines
from .state import RuntimeState, StateStore
from .trading import LOT_SIZE, check_sell, limit_price, resolve_quantity
from .watchlist import WatchlistStore

LOGGER_NAME = "maimai.sim-trader"
DEFAULT_SESSIONS = ("09:30-11:30", "13:00-15:00")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ──── 配置模型 ────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.3.0", description="配置版本")


class MxSectionConfig(PluginConfigBase):
    """妙想接口配置。"""

    __ui_label__ = "妙想"
    __ui_icon__ = "key-round"
    __ui_order__ = 1

    api_key: str = Field(default="", description="东方财富妙想 API Key（mkt_v_ 开头）")
    base_url: str = Field(default=DEFAULT_BASE_URL, description="妙想服务地址")
    timeout: float = Field(default=20.0, ge=1.0, le=120.0, description="单次请求超时，秒")


class AccountSectionConfig(PluginConfigBase):
    """账户与风控配置。"""

    __ui_label__ = "账户与风控"
    __ui_icon__ = "shield-check"
    __ui_order__ = 2

    label: str = Field(default="模拟组合9072966", description="妙想模拟组合名称，用于播报和日志")
    slippage: float = Field(default=0.003, ge=0.0, le=0.02, description="限价单让价比例，0.003 即 0.3%")
    max_position_ratio: float = Field(default=0.2, ge=0.05, le=1.0, description="单票市值占总资产上限")
    min_cash_reserve_ratio: float = Field(default=0.2, ge=0.0, le=0.9, description="买入后至少保留的现金比例")
    max_trades_per_day: int = Field(default=6, ge=1, le=30, description="单日最多下几笔单，防止上头")


class ScheduleSectionConfig(PluginConfigBase):
    """节奏配置。"""

    __ui_label__ = "节奏"
    __ui_icon__ = "clock"
    __ui_order__ = 3

    enabled: bool = Field(default=True, description="是否自动看盘（关掉后只能手动触发）")
    sessions: List[str] = Field(
        default_factory=lambda: list(DEFAULT_SESSIONS),
        description="交易时段，格式 HH:MM-HH:MM",
    )
    default_interval_minutes: int = Field(default=30, ge=5, le=240, description="模型没给下次间隔时的默认值")
    min_interval_minutes: int = Field(default=10, ge=1, le=240, description="两次看盘的最小间隔")
    max_interval_minutes: int = Field(default=240, ge=5, le=600, description="两次看盘的最大间隔")
    idle_sleep_seconds: int = Field(default=60, ge=10, le=600, description="后台循环的空转间隔")
    startup_delay_seconds: int = Field(default=20, ge=0, le=600, description="插件加载后延迟多久开始")


class ModelSectionConfig(PluginConfigBase):
    """模型配置。"""

    __ui_label__ = "模型"
    __ui_icon__ = "brain"
    __ui_order__ = 4

    decision_task_name: str = Field(default="planner", description="决策用的模型任务名，留空用宿主默认")
    decision_model: str = Field(default="", description="决策用的具体模型名，留空跟随任务")
    report_task_name: str = Field(default="replyer", description="播报用的模型任务名，留空用宿主默认")
    report_model: str = Field(default="", description="播报用的具体模型名，留空跟随任务")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0, description="决策温度")
    max_tokens: int = Field(default=900, ge=200, le=4000, description="决策输出上限")


class NotifySectionConfig(PluginConfigBase):
    """播报配置。"""

    __ui_label__ = "播报"
    __ui_icon__ = "megaphone"
    __ui_order__ = 5

    platform: str = Field(default="qq", description="目标平台")
    account_id: str = Field(default="", description="只认这个账号下的聊天流，留空不限")
    group_ids: List[str] = Field(default_factory=lambda: ["982628545"], description="播报到的群号")
    private_user_ids: List[str] = Field(default_factory=lambda: ["1215349250"], description="播报到的私聊用户")
    private_enabled: bool = Field(default=False, description="是否启用私聊播报（功能已做好，先不开）")
    group_enabled: bool = Field(default=True, description="是否启用群播报")
    close_report_enabled: bool = Field(default=True, description="收盘是否发一次复盘")
    close_report_after: str = Field(default="15:00", description="几点之后允许发收盘复盘")


class PersonaSectionConfig(PluginConfigBase):
    """人设配置。"""

    __ui_label__ = "人设"
    __ui_icon__ = "user-round"
    __ui_order__ = 6

    name: str = Field(default="麦麦", description="播报和决策时使用的自称；换成你自己的 bot 名字")


class SimTraderConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    mx: MxSectionConfig = Field(default_factory=MxSectionConfig)
    account: AccountSectionConfig = Field(default_factory=AccountSectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    model: ModelSectionConfig = Field(default_factory=ModelSectionConfig)
    notify: NotifySectionConfig = Field(default_factory=NotifySectionConfig)
    persona: PersonaSectionConfig = Field(default_factory=PersonaSectionConfig)


# ──── 时间与时段 ────


def parse_sessions(raw: Sequence[str]) -> List[Tuple[dt_time, dt_time]]:
    """把 "09:30-11:30" 解析成时间区间。"""

    sessions: List[Tuple[dt_time, dt_time]] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        start_text, sep, end_text = text.partition("-")
        if not sep:
            raise ValueError(f"交易时段格式应为 HH:MM-HH:MM，收到 {text!r}")
        start = dt_time.fromisoformat(start_text.strip())
        end = dt_time.fromisoformat(end_text.strip())
        if end <= start:
            raise ValueError(f"交易时段结束时间必须晚于开始时间：{text!r}")
        sessions.append((start, end))
    return sessions


def in_sessions(now: datetime, sessions: Sequence[Tuple[dt_time, dt_time]]) -> bool:
    """当前是否落在任一交易时段内。"""

    current = now.time()
    return any(start <= current <= end for start, end in sessions)


# ──── 插件主体 ────


class SimTraderPlugin(MaiBotPlugin):
    """麦麦模拟盘插件。"""

    config_model = SimTraderConfig

    def __init__(self) -> None:
        super().__init__()
        self._task: Optional[asyncio.Task[None]] = None
        self._mx: Optional[MxClient] = None
        self._quotes: Optional[TencentQuoteClient] = None
        self._watchlist: Optional[WatchlistStore] = None
        self._state: Optional[StateStore] = None
        self._group_stream_cache: Dict[str, str] = {}
        self._private_stream_cache: Dict[str, str] = {}
        self._busy = False

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """初始化落盘目录、客户端与后台循环。"""

        data_dir = self.ctx.paths.data_dir
        self._watchlist = WatchlistStore(data_dir / "watchlist.json")
        self._state = StateStore(data_dir / "state.json")
        self._watchlist.load()
        self._state.load()
        self.ctx.logger.info(
            "麦麦模拟盘已加载：池子 %d 只，数据目录 %s",
            len(self._watchlist.all()),
            data_dir,
        )
        await self._restart_loop()

    async def on_unload(self) -> None:
        """停掉循环、关掉连接。"""

        await self._stop_loop()
        if self._mx is not None:
            await self._mx.close()
            self._mx = None
        if self._quotes is not None:
            await self._quotes.close()
            self._quotes = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热更新：重建客户端并重启循环。"""

        del config_data
        self.ctx.logger.info("麦麦模拟盘收到配置更新: scope=%s version=%s", scope, version)
        if scope == "self":
            if self._mx is not None:
                await self._mx.close()
                self._mx = None
            await self._restart_loop()

    # ──── 客户端 ────

    def _require_mx(self) -> MxClient:
        """按当前配置拿妙想客户端；配置留空时读环境变量 MX_APIKEY。"""

        api_key = self.config.mx.api_key.strip() or os.environ.get("MX_APIKEY", "").strip()
        if not api_key:
            raise RuntimeError("未配置妙想 API Key（[mx] api_key 或环境变量 MX_APIKEY）")
        if self._mx is None:
            self._mx = MxClient(
                api_key,
                base_url=self.config.mx.base_url,
                timeout=self.config.mx.timeout,
                logger=self.ctx.logger,
            )
        return self._mx

    def _require_quotes(self) -> TencentQuoteClient:
        """拿行情客户端。"""

        if self._quotes is None:
            self._quotes = TencentQuoteClient()
        return self._quotes

    def _require_watchlist(self) -> WatchlistStore:
        """拿股票池。"""

        if self._watchlist is None:
            raise RuntimeError("股票池尚未初始化")
        return self._watchlist

    def _require_state(self) -> StateStore:
        """拿状态。"""

        if self._state is None:
            raise RuntimeError("状态尚未初始化")
        return self._state

    # ──── 后台循环 ────

    async def _restart_loop(self) -> None:
        """按配置启用/停用后台循环。"""

        await self._stop_loop()
        if not self.config.plugin.enabled or not self.config.schedule.enabled:
            self.ctx.logger.info("麦麦模拟盘后台循环未启动（插件或节奏未启用）")
            return
        self._task = asyncio.create_task(self._run_loop(), name="maimai_sim_trader.loop")
        self.ctx.logger.info("麦麦模拟盘后台循环已启动")

    async def _stop_loop(self) -> None:
        """停止后台循环。"""

        if self._task is None:
            return
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run_loop(self) -> None:
        """每分钟醒一次，到点就看盘。"""

        if self.config.schedule.startup_delay_seconds > 0:
            await asyncio.sleep(self.config.schedule.startup_delay_seconds)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.error("麦麦模拟盘循环出错", exc_info=True)
            await asyncio.sleep(self.config.schedule.idle_sleep_seconds)

    async def _tick(self) -> None:
        """判断这一分钟该不该干活。"""

        if self._busy:
            return
        now = datetime.now()
        store = self._require_state()
        state = store.load()
        today = now.strftime("%Y-%m-%d")
        if state.roll_date(today):
            store.save()

        if now.weekday() >= 5:
            return

        sessions = parse_sessions(self.config.schedule.sessions)
        if not in_sessions(now, sessions):
            await self._maybe_send_close_report(now, today)
            return

        if time.time() < state.next_check_at:
            return
        try:
            await self._run_round(now, today)
        except Exception:
            # 出错也要把下次时间推到最小间隔之后，否则会一分钟一轮地空转烧模型
            state.next_check_at = time.time() + self.config.schedule.min_interval_minutes * 60
            store.save()
            raise

    # ──── 一轮决策 ────

    async def _run_round(self, now: datetime, today: str, *, dry_run: bool = False) -> List[str]:
        """完整的一轮：拉数据 → 决策 → 执行 → 播报。

        dry_run=True 时只走到决策和测算：不下单、不写状态、不播报。
        """

        self._busy = True
        store = self._require_state()
        state = store.load()
        try:
            mx = self._require_mx()
            balance = await mx.get_balance()
            holdings = parse_holdings(await mx.get_positions())
            orders = await mx.get_orders()
            quotes = await self._fetch_pool_quotes()
            if not quotes:
                raise RuntimeError("股票池行情为空，无法决策")
            if not dry_run:
                state.track_position_state(has_position=bool(holdings), today=today)
            quote_times = {quote.updated_at[:8] for quote in quotes.values() if quote.updated_at}
            if quote_times and today.replace("-", "") not in quote_times:
                self.ctx.logger.info("行情最新日期是 %s，非交易日", sorted(quote_times))
                if not dry_run:
                    state.next_check_at = time.time() + self.config.schedule.max_interval_minutes * 60
                    store.save()
                    return []

            decision = await self._ask_model(
                now=now,
                balance=balance,
                positions=list(holdings.values()),
                orders=orders,
                quotes=quotes,
            )
            executed, round_notes, snapshot_lines = await self._execute_decision(
                decision=decision,
                balance=balance,
                positions=holdings,
                quotes=quotes,
                today=today,
                state=state,
                dry_run=dry_run,
            )
            if dry_run:
                return round_notes
            interval = max(
                self.config.schedule.min_interval_minutes,
                min(self.config.schedule.max_interval_minutes, decision.next_check_minutes),
            )
            state.record_round(
                note=self._round_note(now, decision.actions, executed),
                mood=decision.mood,
                reasoning=decision.reasoning,
                trade_count=executed,
            )
            state.next_check_at = time.time() + interval * 60
            store.save()

            if executed > 0 or decision.speech.strip():
                await self._broadcast(
                    decision=decision,
                    snapshot_lines=snapshot_lines,
                    round_lines=round_notes,
                )
            return round_notes
        finally:
            self._busy = False

    async def _fetch_pool_quotes(self) -> Dict[str, Quote]:
        """拉股票池的实时快照。"""

        codes = self._require_watchlist().codes()
        if not codes:
            return {}
        return await self._require_quotes().fetch(codes)

    async def _ask_model(
        self,
        *,
        now: datetime,
        balance: Dict[str, Any],
        positions: Sequence[Holding],
        orders: Sequence[Dict[str, Any]],
        quotes: Dict[str, Quote],
    ) -> Decision:
        """把现状交给模型，拿回结构化决策。"""

        store = self._require_state()
        state = store.load()
        pool_items = self._require_watchlist().all()
        pool_lines: List[str] = []
        for item in pool_items:
            quote = quotes.get(item.code)
            pool_lines.append(quote.to_brief() if quote else f"{item.code} {item.name}（暂无行情）")
        pending = [
            f"{row.get('stockCode') or row.get('code')} {row.get('stockName') or ''} "
            f"{row.get('direction') or row.get('orderDrt') or ''} {row.get('quantity') or ''}股 "
            f"@{row.get('price') or row.get('orderPrice') or ''} 状态{row.get('status') or ''}"
            for row in orders
        ]
        messages = build_messages(
            now_text=now.strftime(TIME_FORMAT),
            session_label=self.config.account.label,
            balance=balance,
            positions=positions,
            pool_lines=pool_lines,
            pending_orders=pending,
            recent_notes=state.recent_notes,
            idle_note=self._idle_note(state, now.strftime("%Y-%m-%d")),
            bot_name=self.config.persona.name,
        )
        result = await self.ctx.llm.generate(
            messages,
            task_name=self.config.model.decision_task_name or None,
            model_name=self.config.model.decision_model or "",
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
        )
        if not result.get("success"):
            raise RuntimeError(f"模型调用失败: {result.get('error') or result}")
        text = str(result.get("response") or "")
        decision = parse_decision(text)
        self.ctx.logger.info(
            "决策完成：心情=%s 动作=%d 下次=%d 分钟 理由=%s",
            decision.mood,
            len(decision.actions),
            decision.next_check_minutes,
            decision.reasoning,
        )
        return decision

    async def _execute_decision(
        self,
        *,
        decision: Decision,
        balance: Dict[str, Any],
        positions: Dict[str, Holding],
        quotes: Dict[str, Quote],
        today: str,
        state: RuntimeState,
        dry_run: bool = False,
    ) -> Tuple[int, List[str], List[str]]:
        """按决策下单；返回成交尝试数、文字记录、账户摘要。"""

        mx = self._require_mx()
        watchlist = self._require_watchlist()
        notes: List[str] = []
        executed = 0
        traded_today = state.trade_count

        if not dry_run:
            for code, name in decision.pool_change.add:
                if watchlist.add(code, name, note=f"模型于 {today} 自主加入"):
                    notes.append(f"把 {name}({code}) 加进了池子")
                else:
                    notes.append(f"{name}({code}) 本来就在池子里")
            for code in decision.pool_change.remove:
                removed = watchlist.remove(code)
                if removed is not None:
                    notes.append(f"把 {removed.name}({code}) 踢出了池子")
        else:
            # 试跑不留副作用：池子变更也只写进记录，不落盘
            for code, name in decision.pool_change.add:
                notes.append(f"（试跑）会加入 {name}({code})")
            for code in decision.pool_change.remove:
                notes.append(f"（试跑）会移除 {code}")

        total_assets = _to_float(balance.get("totalAssets"))
        available_cash = _to_float(balance.get("availBalance"))

        for action in decision.actions:
            if traded_today >= self.config.account.max_trades_per_day:
                notes.append(f"今天已经下过 {traded_today} 笔，这笔 {action.code} 先忍住")
                break
            quote = quotes.get(action.code)
            if quote is None:
                notes.append(f"{action.name}({action.code}) 没有行情，跳过")
                continue
            price = limit_price(quote.price, direction=action.action, slippage=self.config.account.slippage)
            holding = find_holding(positions, action.code)
            if action.action == "buy":
                position_value = holding.market_value if holding else 0.0
                quantity = resolve_quantity(
                    requested=action.quantity,
                    price=price,
                    available_cash=available_cash,
                    total_assets=total_assets,
                    current_position_value=position_value,
                    max_position_ratio=self.config.account.max_position_ratio,
                    min_cash_reserve_ratio=self.config.account.min_cash_reserve_ratio,
                )
                if quantity is None:
                    notes.append(f"想买 {action.name}，但资金或仓位不够，没买")
                    continue
            else:
                available_qty = holding.available if holding else 0
                quantity = int(action.quantity // LOT_SIZE) * LOT_SIZE
                verdict = check_sell(quantity=quantity, available_quantity=available_qty)
                if not verdict.ok:
                    notes.append(f"想卖 {action.name}：{verdict.reason}")
                    continue

            if dry_run:
                executed += 1
                verb = "买" if action.action == "buy" else "卖"
                notes.append(f"（试跑）会{verb} {action.name}({action.code}) {quantity}股 @{price:.2f}")
                if action.action == "buy":
                    available_cash -= price * quantity
                continue

            try:
                order_result = await mx.place_order(
                    direction=action.action,
                    stock_code=action.code,
                    price=price,
                    quantity=quantity,
                )
            except (MxApiError, ValueError) as exc:
                notes.append(f"{action.name}({action.code}) 下单失败：{exc}")
                self.ctx.logger.warning("下单失败 %s %s: %s", action.action, action.code, exc)
                continue
            if order_result:
                self.ctx.logger.info("妙想下单返回 %s: %s", action.code, order_result)

            executed += 1
            traded_today += 1
            verb = "买" if action.action == "buy" else "卖"
            notes.append(f"{verb}了 {action.name}({action.code}) {quantity}股 @{price:.2f}")
            self.ctx.logger.info("下单成功 %s %s %d股 @%.2f", action.action, action.code, quantity, price)
            if action.action == "buy":
                available_cash -= price * quantity

        snapshot_lines = build_snapshot_lines(balance, list(positions.values()))
        return executed, notes, snapshot_lines

    def _idle_note(self, state: RuntimeState, today: str) -> str:
        """空仓天数提示，塞进决策 prompt，防止她一直躺着不动。"""

        if not state.empty_since:
            return ""
        days = state.empty_days(today)
        if days <= 0:
            return ""
        return f"已经连续空仓 {days} 天（从 {state.empty_since} 起）"

    def _round_note(self, now: datetime, actions: Sequence[Any], executed: int) -> str:
        """给后续 prompt 用的一行记录。"""

        if executed:
            return f"{now.strftime('%H:%M')} 动了 {executed} 笔"
        if actions:
            return f"{now.strftime('%H:%M')} 想动但没成"
        return f"{now.strftime('%H:%M')} 没动"

    # ──── 播报 ────

    async def _broadcast(
        self,
        *,
        decision: Decision,
        snapshot_lines: Sequence[str],
        round_lines: Sequence[str],
    ) -> None:
        """把这一轮的情况发出去。"""

        text = ""
        try:
            messages = build_report_messages(
                bot_name=self.config.persona.name,
                where="群里",
                group_style=True,
                snapshot_lines=snapshot_lines,
                round_lines=round_lines,
                mood=decision.mood,
                reasoning=decision.reasoning,
            )
            result = await self.ctx.llm.generate(
                messages,
                task_name=self.config.model.report_task_name or None,
                model_name=self.config.model.report_model or "",
                temperature=0.9,
                max_tokens=500,
            )
            if result.get("success"):
                text = str(result.get("response") or "").strip()
        except Exception:
            self.ctx.logger.error("生成播报文案失败，改用纯文本", exc_info=True)
        if not text:
            text = build_plain_fallback(snapshot_lines=snapshot_lines, mood=decision.mood, reasoning=decision.reasoning)
        await self._send_to_targets(text)

    async def _send_to_targets(self, text: str) -> None:
        """按配置把消息发到群和（可选的）私聊。"""

        if not text.strip():
            return
        if self.config.notify.group_enabled:
            for group_id in self.config.notify.group_ids:
                stream_id = await self._resolve_group_stream(group_id)
                if not stream_id:
                    self.ctx.logger.warning("找不到群 %s 的聊天流，跳过播报", group_id)
                    continue
                await self.ctx.send.text(text, stream_id)
        if self.config.notify.private_enabled:
            for user_id in self.config.notify.private_user_ids:
                stream_id = await self._resolve_private_stream(user_id)
                if not stream_id:
                    self.ctx.logger.warning("找不到用户 %s 的私聊流，跳过播报", user_id)
                    continue
                await self.ctx.send.text(text, stream_id)

    async def _resolve_group_stream(self, group_id: str) -> Optional[str]:
        """群号 → 真实 session_id。"""

        if group_id in self._group_stream_cache:
            return self._group_stream_cache[group_id]
        streams = await self.ctx.chat.get_group_streams(self.config.notify.platform)
        matches = [
            row
            for row in _as_rows(streams)
            if str(row.get("group_id") or "") == group_id
            and (not self.config.notify.account_id or str(row.get("account_id") or "") == self.config.notify.account_id)
        ]
        if len(matches) != 1:
            self.ctx.logger.warning("群 %s 命中 %d 条聊天流，无法确定目标", group_id, len(matches))
            return None
        stream_id = str(matches[0].get("session_id") or "")
        if stream_id:
            self._group_stream_cache[group_id] = stream_id
        return stream_id or None

    async def _resolve_private_stream(self, user_id: str) -> Optional[str]:
        """用户号 → 真实私聊 session_id。"""

        if user_id in self._private_stream_cache:
            return self._private_stream_cache[user_id]
        streams = await self.ctx.chat.get_private_streams(self.config.notify.platform)
        matches = [
            row
            for row in _as_rows(streams)
            if str(row.get("user_id") or "") == user_id
            and (not self.config.notify.account_id or str(row.get("account_id") or "") == self.config.notify.account_id)
        ]
        if len(matches) != 1:
            self.ctx.logger.warning("用户 %s 命中 %d 条私聊流，无法确定目标", user_id, len(matches))
            return None
        stream_id = str(matches[0].get("session_id") or "")
        if stream_id:
            self._private_stream_cache[user_id] = stream_id
        return stream_id or None

    async def _maybe_send_close_report(self, now: datetime, today: str) -> None:
        """收盘后发一次复盘（每天一次）。"""

        if not self.config.notify.close_report_enabled:
            return
        after = dt_time.fromisoformat(self.config.notify.close_report_after)
        if now.time() < after:
            return
        state = self._require_state().load()
        if today in state.reported_dates:
            return
        try:
            mx = self._require_mx()
            balance = await mx.get_balance()
            holdings = parse_holdings(await mx.get_positions())
        except Exception:
            self.ctx.logger.error("收盘复盘拉取账户失败", exc_info=True)
            return
        snapshot_lines = build_snapshot_lines(balance, list(holdings.values()))
        round_lines = [f"今天看了 {state.check_count} 轮，动了 {state.trade_count} 笔"]
        decision = Decision(mood=state.last_mood or "平静", reasoning=state.last_reasoning or "今天就这么过去了")
        await self._broadcast(decision=decision, snapshot_lines=snapshot_lines, round_lines=round_lines)
        state.mark_reported(today)
        self._require_state().save()

    # ──── 命令 ────

    @Command(
        "stock",
        description="模拟盘：查看账户、股票池，或增删池子里的票",
        pattern=r"(?s)^/(?:股|stock)(?![0-9A-Za-z_])(?P<rest>.*)$",
    )
    async def handle_stock(self, **kwargs: Any) -> Tuple[bool, Optional[str], bool]:
        """处理 /股 系列命令。"""

        context = self._invocation_context(kwargs)
        rest = str((kwargs.get("matched_groups") or {}).get("rest") or "").strip()
        if not rest or rest in {"状态", "status", "账户"}:
            await self._send_status(context["stream_id"])
            return True, None, True
        if rest in {"池", "池子", "pool"}:
            await self._send_pool(context["stream_id"])
            return True, None, True
        if rest in {"试跑", "dry", "演练"}:
            await self._handle_dry_run(context["stream_id"])
            return True, None, True
        if rest.startswith(("加 ", "add ")):
            await self._handle_pool_edit(context["stream_id"], rest.split(" ", 1)[1], add=True)
            return True, None, True
        if rest.startswith(("减 ", "删 ", "remove ", "del ")):
            await self._handle_pool_edit(context["stream_id"], rest.split(" ", 1)[1], add=False)
            return True, None, True
        await self._send(context["stream_id"], "用法：/股 状态｜/股 池｜/股 加 600519 贵州茅台｜/股 减 600519")
        return False, None, True

    async def _send_status(self, stream_id: str) -> None:
        """回一条账户现状。"""

        try:
            mx = self._require_mx()
            balance = await mx.get_balance()
            holdings = parse_holdings(await mx.get_positions())
        except Exception as exc:
            await self._send(stream_id, f"读账户失败：{exc}")
            return
        lines = build_snapshot_lines(balance, list(holdings.values()))
        await self._send(stream_id, "\n".join(lines))

    async def _send_pool(self, stream_id: str) -> None:
        """回一条股票池清单。"""

        items = self._require_watchlist().all()
        if not items:
            await self._send(stream_id, "池子是空的。")
            return
        lines = [f"股票池（{len(items)} 只）："]
        lines.extend(f"{item.code} {item.name}" for item in items)
        await self._send(stream_id, "\n".join(lines))

    async def _handle_dry_run(self, stream_id: str) -> None:
        """立刻跑一轮决策，但不下单、不播报、不写状态。"""

        await self._send(stream_id, "麦麦去看一眼盘，稍等一下……")
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        try:
            notes = await self._run_round(now, today, dry_run=True)
        except Exception as exc:
            self.ctx.logger.error("试跑失败", exc_info=True)
            await self._send(stream_id, f"试跑失败了：{exc}")
            return
        if not notes:
            await self._send(stream_id, "这轮她什么都没打算做。")
            return
        await self._send(stream_id, "试跑结果（没有真下单）：\n" + "\n".join(notes))

    async def _handle_pool_edit(self, stream_id: str, argument: str, *, add: bool) -> None:
        """增删池子。"""

        parts = argument.split()
        if not parts:
            await self._send(stream_id, "要加/减哪只？格式：600519 贵州茅台")
            return
        code = parts[0].strip()
        name = " ".join(parts[1:]).strip() or code
        watchlist = self._require_watchlist()
        if add:
            created = watchlist.add(code, name, note="主人在聊天里加的" if name else "")
            await self._send(stream_id, f"已把 {name}({code}) 加进池子。" if created else f"{name}({code}) 已经在池子里了。")
            return
        removed = watchlist.remove(code)
        if removed is None:
            await self._send(stream_id, f"池子里没有 {code}。")
            return
        await self._send(stream_id, f"已把 {removed.name}({code}) 踢出池子。")

    # ──── 工具 ────

    @Tool(
        "stock_account_summary",
        description="查看模拟盘账户的资产、持仓和盈亏",
        parameters=[],
        chat_scope="private",
    )
    async def tool_account_summary(self, **kwargs: Any) -> Dict[str, Any]:
        """给她在聊天里回答"我们最近炒得怎么样"。"""

        del kwargs
        try:
            mx = self._require_mx()
            balance = await mx.get_balance()
            holdings = parse_holdings(await mx.get_positions())
        except Exception as exc:
            return {"success": False, "content": f"查不到账户：{exc}"}
        return {"success": True, "content": "\n".join(build_snapshot_lines(balance, list(holdings.values())))}

    # ──── 工具方法 ────

    def _invocation_context(self, kwargs: Dict[str, Any]) -> Dict[str, str]:
        """从命令参数里取出会话上下文。"""

        return {
            "stream_id": str(kwargs.get("stream_id") or "").strip(),
            "platform": str(kwargs.get("platform") or self.config.notify.platform).strip(),
            "user_id": str(kwargs.get("user_id") or "").strip(),
            "group_id": str(kwargs.get("group_id") or "").strip(),
        }

    async def _send(self, stream_id: str, text: str) -> None:
        """发一条文本。"""

        if not stream_id or not text.strip():
            return
        await self.ctx.send.text(text, stream_id)


# ──── 小工具 ────


def _as_rows(value: Any) -> List[Dict[str, Any]]:
    """把能力返回的聊天流结果规整成列表。"""

    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("streams", "data", "items"):
            rows = value.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _to_float(value: Any) -> float:
    """取浮点，拿不到就是 0。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def create_plugin() -> SimTraderPlugin:
    """Runner 入口。"""

    return SimTraderPlugin()


__all__ = ["SimTraderConfig", "SimTraderPlugin", "create_plugin", "in_sessions", "parse_sessions"]
