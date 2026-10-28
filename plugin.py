"""模拟盘（sim-trader）插件入口。

让它用自己的判断去炒一个东财妙想模拟组合，然后在指定群/私聊里汇报盈亏。
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
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from .config_ui import build_config_ui_schema
from .decision import Decision, build_messages, parse_decision
from .mx_client import DEFAULT_BASE_URL, MxApiError, MxClient
from .positions import Holding, find_holding, parse_holdings
from .query_args import QueryParseError, parse_rate_argument, parse_stock_query
from .quotes import Quote, TencentQuoteClient
from .report import build_plain_fallback, build_report_messages, build_snapshot_lines
from .state import (
    HISTORY_IN_PROMPT,
    HISTORY_IN_REPLY,
    MAX_ROUND_ACTIONS,
    RoundRecord,
    RuntimeState,
    StateStore,
)
from .trading import LOT_SIZE, check_sell, limit_price, resolve_quantity
from .watchlist import WatchlistStore, is_valid_code

LOGGER_NAME = "maimai.sim-trader"
DEFAULT_BOT_NAME = "麦麦"
DEFAULT_SESSIONS = ("09:30-11:30", "13:00-15:00")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


# ──── 配置模型 ────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.7.6", description="配置版本")


class MxSectionConfig(PluginConfigBase):
    """妙想接口配置。"""

    __ui_label__ = "妙想"
    __ui_icon__ = "key-round"
    __ui_order__ = 1

    api_key: str = Field(default="", description="东方财富妙想 API Key（mkt_v_ 开头）")
    base_url: str = Field(
        default=DEFAULT_BASE_URL,
        description="妙想服务地址，必须是 https；不建议指向本机、内网或云元数据地址",
    )
    timeout: float = Field(default=20.0, ge=1.0, le=120.0, description="单次请求超时，秒")


class AccountSectionConfig(PluginConfigBase):
    """账户与风控配置。"""

    __ui_label__ = "账户与风控"
    __ui_icon__ = "shield-check"
    __ui_order__ = 2

    label: str = Field(default="", description="账户显示名；留空则自动读妙想返回的组合名")
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
    manual_cooldown_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="同一聊天流两次手动看盘（/股 试跑、sim_trader_review）之间的最小间隔；0 表示不限",
    )


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
    max_tokens: int = Field(
        default=0,
        ge=0,
        le=16000,
        description=(
            "决策输出上限；0 表示跟随宿主任务配置（推荐）。"
            "填了就会覆盖宿主设置——推理模型（reasoning effort）思考也占额度，给小了正文会空"
        ),
    )
    report_max_tokens: int = Field(
        default=0,
        ge=0,
        le=16000,
        description=(
            "播报输出上限；0 表示跟随宿主任务配置（推荐）。"
            "推理模型写播报也要先思考，覆盖成小数值会让这一轮播报直接失败"
        ),
    )
    request_timeout_ms: int = Field(
        default=180000,
        ge=10000,
        le=600000,
        description="单次模型调用允许等待的毫秒数；宿主默认 30 秒，思考型模型经常不够",
    )


class NotifySectionConfig(PluginConfigBase):
    """播报配置。"""

    __ui_label__ = "播报"
    __ui_icon__ = "megaphone"
    __ui_order__ = 5

    platform: str = Field(default="qq", description="目标平台")
    account_id: str = Field(default="", description="只认这个账号下的聊天流，留空不限")
    group_ids: List[str] = Field(default_factory=list, description="播报到的群号；留空表示不播报到任何群")
    private_user_ids: List[str] = Field(default_factory=list, description="播报到的私聊用户；需 private_enabled")
    private_enabled: bool = Field(default=False, description="是否启用私聊播报（功能已做好，先不开）")
    group_enabled: bool = Field(default=True, description="是否启用群播报")
    close_report_enabled: bool = Field(default=True, description="收盘是否发一次复盘")
    close_report_after: str = Field(default="15:00", description="几点之后允许发收盘复盘")


class OwnerSectionConfig(PluginConfigBase):
    """主人白名单。"""

    __ui_label__ = "主人"
    __ui_icon__ = "user-check"
    __ui_order__ = 6

    owners: List[str] = Field(
        default_factory=list,
        description="能改动股票池和看盘节奏的用户，格式 platform:user_id；留空表示只有查询类命令可用",
    )


class RiskSectionConfig(PluginConfigBase):
    """决策大胆程度。"""

    __ui_label__ = "风险偏好"
    __ui_icon__ = "flame"
    __ui_order__ = 7

    profile: str = Field(
        default="steady",
        description=(
            "决策大胆程度：steady 稳健 / aggressive 激进 / yolo 梭哈。"
            "档位以 [account] 里的数字为基准按倍数调整，再夹到代码里的绝对上限；"
            "写错了会直接报错，不会静默退回默认档"
        ),
    )


class SimTraderConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    mx: MxSectionConfig = Field(default_factory=MxSectionConfig)
    account: AccountSectionConfig = Field(default_factory=AccountSectionConfig)
    risk: RiskSectionConfig = Field(default_factory=RiskSectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    model: ModelSectionConfig = Field(default_factory=ModelSectionConfig)
    notify: NotifySectionConfig = Field(default_factory=NotifySectionConfig)
    owner: OwnerSectionConfig = Field(default_factory=OwnerSectionConfig)


# ──── 风险档位 ────

# 档位只做「倍数调整」，基准仍然是 [account] 里用户自己填的数字：
# 这样老配置升级上来行为不变（稳健 = ×1.0），想调单个数字也照样生效。
RISK_PROFILES: Dict[str, Dict[str, Any]] = {
    "steady": {
        "label": "稳健",
        "position_multiplier": 1.0,
        "cash_multiplier": 1.0,
        "trades_multiplier": 1.0,
        "slippage_delta": 0.0,
        "style": (
            "这一轮按稳健档来：许可窄、额度小，只有理由清楚到能一句话讲明白才动手；"
            "没把握就空着，空着不算失职，宁可少动也别追高、别补跌。"
        ),
    },
    "aggressive": {
        "label": "激进",
        "position_multiplier": 1.4,
        "cash_multiplier": 0.5,
        "trades_multiplier": 1.8,
        "slippage_delta": 0.001,
        "style": (
            "这一轮按激进档来：额度放宽了，能动的笔数也更多，允许比平时更集中；"
            "看到机会不用等它完美，但每笔都要说得出「为什么是现在」，说不出来就别动。"
        ),
    },
    "yolo": {
        "label": "梭哈",
        "position_multiplier": 1.8,
        "cash_multiplier": 0.1,
        "trades_multiplier": 2.0,
        "slippage_delta": 0.002,
        "style": (
            "这一轮按梭哈档来：额度几乎拉满，允许重仓、允许看错，亏的也是模拟盘，图的就是这个；"
            "想干就干，但别为了凑热闹硬编理由，也别把话说满——亏了要认。"
        ),
    },
}

# 绝对上限：档位再怎么调也越不过去，避免一把梭到账户当天失去观赏价值
HARD_MAX_POSITION_RATIO = 0.40
HARD_MAX_TRADES_PER_DAY = 12


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
    """模拟盘插件。"""

    config_model = SimTraderConfig

    @classmethod
    def build_config_schema(cls, **plugin_info: str) -> Dict[str, Any]:
        """只重组 WebUI 的展示结构，配置模型与 TOML 路径保持不变。"""

        return build_config_ui_schema(super().build_config_schema(**plugin_info))

    def __init__(self) -> None:
        super().__init__()
        self._task: Optional[asyncio.Task[None]] = None
        self._mx: Optional[MxClient] = None
        self._quotes: Optional[TencentQuoteClient] = None
        self._watchlist: Optional[WatchlistStore] = None
        self._state: Optional[StateStore] = None
        self._group_stream_cache: Dict[str, str] = {}
        self._private_stream_cache: Dict[str, str] = {}
        self._bot_name = ""
        self._bot_persona = ""
        self._busy = False
        # 手动触发的手动看盘冷却：{聊天流: 上次触发时间}
        self._manual_runs: Dict[str, float] = {}

    # ──── 生命周期 ────

    async def on_load(self) -> None:
        """初始化落盘目录、读取宿主人设、启动后台循环。"""

        data_dir = self.ctx.paths.data_dir
        self._watchlist = WatchlistStore(data_dir / "watchlist.json")
        self._state = StateStore(data_dir / "state.json")
        self._watchlist.load()
        self._state.load()
        await self._load_persona()
        self.ctx.logger.info(
            "模拟盘已加载：池子 %d 只，数据目录 %s",
            len(self._watchlist.all()),
            data_dir,
        )
        self._warn_if_not_configured()
        await self._restart_loop()

    def _warn_if_not_configured(self) -> None:
        """首次下载后最容易漏填的几项一次性说清楚，别让用户以为插件坏了。"""

        if not self._api_key():
            self.ctx.logger.warning(
                "没有配置妙想 API Key：[mx] api_key 为空，环境变量 MX_APIKEY 也没有。"
                "没有 Key 就拿不到账户和行情，每一轮看盘都会失败。"
            )
        notify = self.config.notify
        targets = list(notify.group_ids)
        if notify.private_enabled:
            targets += list(notify.private_user_ids)
        if not targets:
            self.ctx.logger.warning(
                "没有配置播报目标：[notify] group_ids 为空"
                "（私聊播报未启用或 private_user_ids 为空），模拟盘会照常决策，但不会在任何聊天里说话。"
            )
        if not [item.strip() for item in self.config.owner.owners if item.strip()]:
            self.ctx.logger.warning(
                "owner.owners 为空：改动股票池、调整看盘节奏的命令暂时没人能用（查询类命令不受影响）。"
            )

    async def _load_persona(self) -> None:
        """从 MaiBot 本体读取昵称与人设，插件不再自己定义一份。

        读不到时退回通用称呼——人设缺失不该让交易挂掉，但要在日志里说清楚。
        """

        nickname = str(await self.ctx.config.get("bot.nickname", "") or "").strip()
        persona = str(await self.ctx.config.get("personality.personality", "") or "").strip()
        self._bot_name = nickname or DEFAULT_BOT_NAME
        self._bot_persona = persona
        if persona:
            self.ctx.logger.info("已从宿主人设读取：昵称=%s，人设 %d 字", self._bot_name, len(persona))
        else:
            self.ctx.logger.warning(
                "宿主没有配置 personality.personality，播报将使用通用称呼「%s」",
                self._bot_name,
            )

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
        self.ctx.logger.info("模拟盘收到配置更新: scope=%s version=%s", scope, version)
        if scope == "bot":
            await self._load_persona()
        if scope == "self":
            if self._mx is not None:
                await self._mx.close()
                self._mx = None
            await self._restart_loop()

    # ──── 客户端 ────

    def _api_key(self) -> str:
        """当前可用的妙想 API Key：配置优先，其次环境变量。"""

        return self.config.mx.api_key.strip() or os.environ.get("MX_APIKEY", "").strip()

    def _require_mx(self) -> MxClient:
        """按当前配置拿妙想客户端；配置留空时读环境变量 MX_APIKEY。"""

        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("未配置妙想 API Key（[mx] api_key 或环境变量 MX_APIKEY）")
        base_url = (self.config.mx.base_url or DEFAULT_BASE_URL).strip()
        if not base_url.lower().startswith("https://"):
            # 地址是主人自己填的，但 API Key 会跟着请求一起发出去，只允许 https
            raise RuntimeError(f"[mx] base_url 必须是 https 地址，当前是 {base_url!r}")
        if self._mx is None:
            self._mx = MxClient(
                api_key,
                base_url=base_url,
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

    # ──── 风险档位 ────

    def _risk_profile(self) -> Dict[str, Any]:
        """按配置取当前风险档；写错了直接报错，不静默退回默认档。"""

        name = str(self.config.risk.profile or "").strip().lower()
        profile = RISK_PROFILES.get(name)
        if profile is None:
            raise RuntimeError(
                f"[risk] profile 只支持 {' / '.join(RISK_PROFILES)}，当前为 {self.config.risk.profile!r}"
            )
        return profile

    def _effective_limits(self) -> Dict[str, float]:
        """把档位倍数作用到用户配置的数字上，并夹到代码里的绝对上限。"""

        account = self.config.account
        profile = self._risk_profile()
        position = account.max_position_ratio * float(profile["position_multiplier"])
        cash = account.min_cash_reserve_ratio * float(profile["cash_multiplier"])
        trades = round(account.max_trades_per_day * float(profile["trades_multiplier"]))
        slippage = account.slippage + float(profile["slippage_delta"])
        return {
            "max_position_ratio": min(HARD_MAX_POSITION_RATIO, max(0.05, position)),
            "min_cash_reserve_ratio": min(0.9, max(0.0, cash)),
            "max_trades_per_day": float(min(HARD_MAX_TRADES_PER_DAY, max(1, trades))),
            "slippage": min(0.02, max(0.0, slippage)),
        }

    def _risk_summary(self) -> str:
        """给状态回执用的一句话档位说明。"""

        try:
            profile = self._risk_profile()
            limits = self._effective_limits()
        except RuntimeError as exc:
            return f"风险档配置有问题：{exc}"
        return (
            f"风险档：{profile['label']}（单票≤{limits['max_position_ratio']:.0%}，"
            f"现金≥{limits['min_cash_reserve_ratio']:.0%}，每日≤{int(limits['max_trades_per_day'])} 笔）"
        )

    # ──── 手动看盘的闸门 ────

    def _manual_cooldown_left(self, stream_key: str) -> float:
        """同一聊天流还要等多少秒才能再手动看盘；0 表示可以立刻跑。"""

        cooldown = self.config.schedule.manual_cooldown_seconds
        if cooldown <= 0:
            return 0.0
        last = self._manual_runs.get(stream_key, 0.0)
        return max(0.0, cooldown - (time.time() - last))

    def _mark_manual_run(self, stream_key: str) -> None:
        """记下这次手动看盘的时刻，供冷却判断使用。"""

        self._manual_runs[stream_key] = time.time()

    def _in_trading_session(self, now: datetime) -> bool:
        """当前是否落在配置的交易时段内。"""

        return in_sessions(now, parse_sessions(self.config.schedule.sessions))

    # ──── 后台循环 ────

    async def _restart_loop(self) -> None:
        """按配置启用/停用后台循环。"""

        await self._stop_loop()
        if not self.config.plugin.enabled or not self.config.schedule.enabled:
            self.ctx.logger.info("模拟盘后台循环未启动（插件或节奏未启用）")
            return
        if not self._api_key():
            # 没有 Key 就起循环，只会在交易时段每轮抛一次异常刷日志；配置补上后会自动启动
            self.ctx.logger.warning(
                "未配置妙想 API Key（[mx] api_key 或环境变量 MX_APIKEY），模拟盘后台循环不会启动；"
                "填好并保存配置后会自动开始。"
            )
            return
        self._task = asyncio.create_task(self._run_loop(), name="maimai_sim_trader.loop")
        self.ctx.logger.info("模拟盘后台循环已启动")

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
                self.ctx.logger.error("模拟盘循环出错", exc_info=True)
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

    async def _run_round(self, now: datetime, today: str, *, dry_run: bool = False, announce: bool = True) -> List[str]:
        """完整的一轮：拉数据 → 决策 → 执行 → 播报。

        dry_run=True 时只走到决策和测算：不下单、不写状态、不播报。
        announce=False 时照常下单，但不往群里播报（供聊天里的工具调用使用）。
        """

        # 记下进来之前的状态：并发跑两轮时，先结束的那个不该把还在跑的那轮标志清掉
        was_busy = self._busy
        self._busy = True
        store = self._require_state()
        state = store.load()
        # 聊天触发的看盘不走 _tick，这里也要跨日重置，否则「今天已下几笔」会一直累加
        if not dry_run and state.roll_date(today):
            store.save()
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
                record=self._round_record(now, decision, round_notes),
                trade_count=executed,
            )
            if executed > 0:
                # 这一轮建了仓，空仓起点要立刻清掉，别等到下一轮
                state.track_position_state(has_position=True, today=today)
            state.next_check_at = time.time() + interval * 60
            store.save()

            if announce and (executed > 0 or decision.speech.strip()):
                await self._broadcast(
                    decision=decision,
                    snapshot_lines=snapshot_lines,
                    round_lines=round_notes,
                )
            return round_notes
        finally:
            self._busy = was_busy

    async def _fetch_pool_quotes(self) -> Dict[str, Quote]:
        """拉股票池的实时快照。

        池子可能是手改过的，遇到非六位代码就跳过并告警：
        让一个坏代码把整轮行情拉挂掉，比少看一只票糟糕得多。
        """

        codes = self._require_watchlist().codes()
        valid = [code for code in codes if is_valid_code(code)]
        dropped = [code for code in codes if not is_valid_code(code)]
        if dropped:
            self.ctx.logger.warning("股票池里有不合法的代码，已跳过: %s", dropped)
        if not valid:
            return {}
        return await self._require_quotes().fetch(valid)

    async def _llm_generate(
        self,
        *,
        messages: List[Dict[str, str]],
        task_name: str,
        model_name: str,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        """带超时的模型调用。

        SDK 的 ``llm.generate`` 走宿主默认的 30 秒超时，思考型模型经常不够用
        （日志里表现为 ``[E_TIMEOUT] 请求 cap.call 超时``）。这里直接走能力层，
        把超时拉到配置值。
        """

        payload: Dict[str, Any] = {
            "prompt": messages,
            "model_name": model_name or "",
            "temperature": temperature,
        }
        # 0 表示「跟随宿主任务配置」：不自作主张压额度，免得推理模型思考没写完就被截断
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if task_name:
            payload["task_name"] = task_name
        result = await self.ctx.call_capability(
            "llm.generate",
            timeout_ms=self.config.model.request_timeout_ms,
            **payload,
        )
        if not isinstance(result, dict):
            return {"success": False, "error": f"模型返回了非对象结果: {type(result).__name__}"}
        return result

    @staticmethod
    def _llm_failure_hint(result: Dict[str, Any]) -> str:
        """把宿主返回的模型错误翻成用户能动手的方案。

        最常见的一种是推理模型把输出额度全花在思考上（``max_output_tokens``），
        宿主只会给一串英文原因，不翻译的话没人知道该改哪里。
        """

        error = str(result.get("error") or result)
        if "max_output_tokens" in error or "IncompleteDetails" in error:
            return (
                f"模型把输出额度全花在思考上、正文没写出来（{error}）。"
                "把 [model] max_tokens / report_max_tokens 设成 0（跟随宿主任务）或调大，"
                "也可以给这个任务换一个非推理模型。"
            )
        return f"模型调用失败: {error}"

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
        # 档位算出来的限制只算一次，额度块和规则里的数字必须同源
        limits = self._effective_limits()
        messages = build_messages(
            now_text=now.strftime(TIME_FORMAT),
            session_label=self._account_label(balance),
            balance=balance,
            positions=positions,
            pool_lines=pool_lines,
            pending_orders=pending,
            history_blocks=self._history_blocks(state),
            limits_block=build_limits_block(
                limits=limits,
                balance=balance,
                positions=positions,
                trades_used=state.trade_count,
            ),
            idle_note=self._idle_note(state, now.strftime("%Y-%m-%d")),
            bot_name=self._bot_name or DEFAULT_BOT_NAME,
            persona=self._bot_persona,
            risk_style=str(self._risk_profile()["style"]),
            max_position_pct=f"{limits['max_position_ratio']:.0%}",
            min_cash_pct=f"{limits['min_cash_reserve_ratio']:.0%}",
        )
        result = await self._llm_generate(
            messages=messages,
            task_name=self.config.model.decision_task_name,
            model_name=self.config.model.decision_model,
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
        )
        if not result.get("success"):
            raise RuntimeError(self._llm_failure_hint(result))
        text = str(result.get("response") or "")
        if not text.strip():
            # 思考型模型会把额度全花在 reasoning 上，正文一空就必然是 max_tokens 不够
            reasoning_chars = len(str(result.get("reasoning") or ""))
            raise RuntimeError(
                f"模型只输出了思考过程（{reasoning_chars} 字）没有正文，"
                f"把 [model] max_tokens 调大试试（当前 {self.config.model.max_tokens}）"
            )
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
        # 风险档在这里落到具体数字上：档位只调倍数，绝对上限仍由代码夹住
        limits = self._effective_limits()

        for action in decision.actions:
            if traded_today >= limits["max_trades_per_day"]:
                notes.append(
                    f"今天已经下过 {traded_today} 笔，这笔 {action.code} 先忍住"
                    f"（风险档上限 {int(limits['max_trades_per_day'])} 笔）"
                )
                break
            quote = quotes.get(action.code)
            if quote is None:
                notes.append(f"{action.name}({action.code}) 没有行情，跳过")
                continue
            price = limit_price(quote.price, direction=action.action, slippage=limits["slippage"])
            holding = find_holding(positions, action.code)
            if action.action == "buy":
                position_value = holding.market_value if holding else 0.0
                quantity = resolve_quantity(
                    requested=action.quantity,
                    price=price,
                    available_cash=available_cash,
                    total_assets=total_assets,
                    current_position_value=position_value,
                    max_position_ratio=limits["max_position_ratio"],
                    min_cash_reserve_ratio=limits["min_cash_reserve_ratio"],
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

    def _account_label(self, balance: Dict[str, Any]) -> str:
        """账户显示名：配置优先，留空就用妙想返回的组合名。"""

        configured = self.config.account.label.strip()
        if configured:
            return configured
        return str(balance.get("accName") or "").strip() or "模拟盘"

    def _idle_note(self, state: RuntimeState, today: str) -> str:
        """空仓天数提示，塞进决策 prompt，防止她一直躺着不动。"""

        if not state.empty_since:
            return ""
        days = state.empty_days(today)
        if days <= 0:
            return ""
        return f"已经连续空仓 {days} 天（从 {state.empty_since} 起）"

    def _round_record(self, now: datetime, decision: Decision, round_notes: Sequence[str]) -> RoundRecord:
        """把这一轮整理成给后续轮次参考的记录。

        判断只留模型自己写的 reasoning，动作只留实际发生或被拦下的行，
        这样下一轮能看到「上次怎么想、后来怎么落地的」，而不是一串孤立的成交。
        """

        actions = [str(line).strip() for line in round_notes if str(line).strip()]
        # 成交行信息量最大，先把它们挑到前面，别让池子变更把成交挤掉
        trades = [line for line in actions if line.startswith(("买了", "卖了"))]
        others = [line for line in actions if not line.startswith(("买了", "卖了"))]
        return RoundRecord(
            at=now.strftime("%m-%d %H:%M"),
            mood=decision.mood,
            reasoning=decision.reasoning,
            actions=(trades + others)[:MAX_ROUND_ACTIONS],
        )

    def _history_blocks(self, state: RuntimeState) -> List[str]:
        """把最近几轮渲染成决策 prompt 里的块，越靠下越新。"""

        return [record.to_prompt_block() for record in state.round_records[-HISTORY_IN_PROMPT:]]

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
                bot_name=self._bot_name or DEFAULT_BOT_NAME,
                persona=self._bot_persona,
                where="群里",
                group_style=True,
                snapshot_lines=snapshot_lines,
                round_lines=round_lines,
                mood=decision.mood,
                reasoning=decision.reasoning,
            )
            result = await self._llm_generate(
                messages=messages,
                task_name=self.config.model.report_task_name,
                model_name=self.config.model.report_model,
                temperature=0.9,
                max_tokens=self.config.model.report_max_tokens,
            )
            if result.get("success"):
                text = str(result.get("response") or "").strip()
            else:
                self.ctx.logger.warning("播报模型调用失败，改用纯文本: %s", self._llm_failure_hint(result))
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
        description="模拟盘：查账户、池子、委托、最近记录，查单只票行情，或增删池子与调整看盘节奏",
        pattern=r"(?s)^/(?:股|stock)(?![0-9A-Za-z_])(?P<rest>.*)$",
    )
    async def handle_stock(self, **kwargs: Any) -> Tuple[bool, Optional[str], bool]:
        """处理 /股 系列命令。"""

        context = self._invocation_context(kwargs)
        stream_id = context["stream_id"]
        rest = str((kwargs.get("matched_groups") or {}).get("rest") or "").strip()

        if not rest or rest in {"状态", "status", "账户"}:
            await self._send_status(stream_id)
            return True, None, True
        if rest in {"池", "池子", "pool"}:
            await self._send_pool(stream_id)
            return True, None, True
        if rest in {"委托", "委托单", "挂单", "orders"}:
            await self._send_orders(stream_id)
            return True, None, True
        if rest in {"记录", "复盘", "history", "log"}:
            await self._send_history(stream_id)
            return True, None, True
        if rest in {"试跑", "dry", "演练"}:
            await self._handle_dry_run(stream_id)
            return True, None, True
        if rest in {"行情", "quote", "价"}:
            await self._send(stream_id, "用法：/股 行情 600036，或者直接 /股 招商银行")
            return False, None, True

        # 下面三条会改动状态，只认主人
        if rest.startswith(("加 ", "add ")):
            if not self._is_owner(context):
                await self._send(stream_id, "这个操作只有主人能做。")
                return False, None, True
            await self._handle_pool_edit(stream_id, rest.split(" ", 1)[1], add=True)
            return True, None, True
        if rest.startswith(("减 ", "删 ", "remove ", "del ")):
            if not self._is_owner(context):
                await self._send(stream_id, "这个操作只有主人能做。")
                return False, None, True
            await self._handle_pool_edit(stream_id, rest.split(" ", 1)[1], add=False)
            return True, None, True
        if rest.startswith(("节奏 ", "rate ")):
            if not self._is_owner(context):
                await self._send(stream_id, "这个操作只有主人能做。")
                return False, None, True
            await self._handle_rate(stream_id, rest.split(" ", 1)[1])
            return True, None, True

        # 兜底：当成单只票的行情查询，宽松识别代码/名字
        await self._handle_quote(stream_id, rest)
        return True, None, True

    def _is_owner(self, context: Dict[str, str]) -> bool:
        """判断调用者是否在主人白名单里。"""

        platform = context["platform"]
        user_id = context["user_id"]
        if not platform or not user_id:
            return False
        identity = f"{platform}:{user_id}"
        allowed = {item.strip() for item in self.config.owner.owners if item.strip()}
        return identity in allowed

    async def _build_quote_text(self, rest: str) -> str:
        """宽松查询单只票：代码、名字、或者两个一起写都认。"""

        code, name = parse_stock_query(rest)
        watchlist = self._require_watchlist()
        if not code and name:
            matched = watchlist.find_by_name(name)
            if matched is not None:
                code = matched.code
        if not code:
            return (
                f"没认出「{rest}」是哪只票。\n写六位代码最稳（/股 600036），"
                "写池子里的名字也行（/股 池 可以先看有哪些）。"
            )

        try:
            quotes = await self._require_quotes().fetch([code])
        except Exception as exc:
            return f"查行情失败：{exc}"
        quote = quotes.get(code)
        if quote is None:
            return f"{code} 没拿到行情，可能代码不对或者停牌了。"

        lines = [quote.to_brief()]
        item = watchlist.get(code)
        lines.append(f"池子里有它（{item.name}）" if item is not None else "不在股票池里")
        try:
            holdings = parse_holdings(await self._require_mx().get_positions())
        except Exception as exc:
            self.ctx.logger.warning("查行情时读持仓失败：%s", exc)
            holdings = {}
        position = holdings.get(code)
        if position is not None:
            lines.append(
                f"持仓 {position.quantity}股 成本 {position.avg_cost:.2f} 浮盈 {position.profit:,.2f} 元"
            )
        return "\n".join(lines)

    async def _handle_quote(self, stream_id: str, rest: str) -> None:
        """命令通道的行情查询。"""

        await self._send(stream_id, await self._build_quote_text(rest))

    async def _build_status_text(self) -> str:
        """账户现状。"""

        try:
            mx = self._require_mx()
            balance = await mx.get_balance()
            holdings = parse_holdings(await mx.get_positions())
        except Exception as exc:
            return f"读账户失败：{exc}"
        lines = build_snapshot_lines(balance, list(holdings.values()))
        lines.append(self._risk_summary())
        return "\n".join(lines)

    async def _send_status(self, stream_id: str) -> None:
        """回一条账户现状。"""

        await self._send(stream_id, await self._build_status_text())

    def _build_pool_text(self) -> str:
        """股票池清单。"""

        items = self._require_watchlist().all()
        if not items:
            return "池子是空的。"
        lines = [f"股票池（{len(items)} 只）："]
        lines.extend(f"{item.code} {item.name}" for item in items)
        return "\n".join(lines)

    async def _send_pool(self, stream_id: str) -> None:
        """回一条股票池清单。"""

        await self._send(stream_id, self._build_pool_text())

    async def _build_orders_text(self) -> str:
        """当日委托。"""

        try:
            orders = await self._require_mx().get_orders()
        except Exception as exc:
            return f"读委托失败：{exc}"
        if not orders:
            return "今天还没有委托。"
        lines = [f"当日委托（{len(orders)} 条）："]
        for row in orders:
            code = row.get("secCode") or row.get("stockCode") or row.get("code") or ""
            name = row.get("secName") or row.get("stockName") or row.get("name") or ""
            direction = row.get("direction") or row.get("orderDrt") or ""
            quantity = row.get("quantity") or row.get("orderQty") or ""
            price = row.get("price") or row.get("orderPrice") or ""
            status = row.get("status") or row.get("orderStatus") or ""
            lines.append(f"{code} {name} {direction} {quantity}股 @{price} 状态{status}")
        return "\n".join(lines)

    async def _send_orders(self, stream_id: str) -> None:
        """回一条当日委托。"""

        await self._send(stream_id, await self._build_orders_text())

    def _build_history_text(self) -> str:
        """最近几轮判断与动作，按时间从早到晚。"""

        state = self._require_state().load()
        lines = [f"今天看了 {state.check_count} 轮，动了 {state.trade_count} 笔"]
        if state.empty_since:
            lines.append(f"从 {state.empty_since} 起一直空仓")
        if not state.round_records:
            lines.append("还没有记录，等她跑过一轮再来看。")
            return "\n".join(lines)
        lines.append("最近几轮（越靠下越新）：")
        for record in state.round_records[-HISTORY_IN_REPLY:]:
            lines.append("· " + record.to_prompt_block().replace("\n", " / "))
        return "\n".join(lines)

    async def _send_history(self, stream_id: str) -> None:
        """回一条最近几轮的判断记录。"""

        await self._send(stream_id, self._build_history_text())

    async def _handle_rate(self, stream_id: str, argument: str) -> None:
        """调整下次看盘间隔；0 表示下一分钟就看一次。"""

        try:
            minutes = parse_rate_argument(argument)
        except QueryParseError as exc:
            await self._send(stream_id, str(exc))
            return
        store = self._require_state()
        state = store.load()
        state.next_check_at = time.time() + minutes * 60
        store.save()
        if minutes == 0:
            await self._send(stream_id, "好，下一分钟就去看一眼。跑完用 /股 记录 能看到她怎么想的。")
        else:
            await self._send(stream_id, f"好，{minutes} 分钟后再看一眼。")

    async def _handle_dry_run(self, stream_id: str) -> None:
        """立刻跑一轮决策，但不下单、不播报、不写状态。"""

        if self._busy:
            await self._send(stream_id, "已经有一轮在看盘了，等它跑完再说。")
            return
        cooldown_left = self._manual_cooldown_left(stream_id)
        if cooldown_left > 0:
            await self._send(stream_id, f"刚看过一轮，{int(cooldown_left) + 1} 秒后再试。")
            return
        self._mark_manual_run(stream_id)
        await self._send(stream_id, f"{self._bot_name or DEFAULT_BOT_NAME}去看一眼盘，稍等一下……")
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
        "sim_trader_query",
        description="查模拟盘：账户、股票池、当日委托、最近判断、单只票行情",
        parameters=[
            ToolParameterInfo(
                name="action",
                param_type=ToolParamType.STRING,
                description="查什么：status=账户，pool=股票池，orders=当日委托，history=最近几轮的判断，quote=单只票行情",
                enum_values=["status", "pool", "orders", "history", "quote"],
            ),
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="action=quote 时填股票代码或名字（例如 600036 或 招商银行），其他 action 留空",
                required=False,
            ),
        ],
    )
    async def tool_query(self, action: str = "", query: str = "", **kwargs: Any) -> Dict[str, Any]:
        """聊天里查模拟盘的各种状态。"""

        del kwargs
        normalized = action.strip().lower()
        try:
            if normalized == "status":
                return {"success": True, "content": await self._build_status_text()}
            if normalized == "pool":
                return {"success": True, "content": self._build_pool_text()}
            if normalized == "orders":
                return {"success": True, "content": await self._build_orders_text()}
            if normalized == "history":
                return {"success": True, "content": self._build_history_text()}
            if normalized == "quote":
                if not query.strip():
                    return {"success": False, "content": "查行情要给个股票代码或名字。"}
                return {"success": True, "content": await self._build_quote_text(query)}
        except Exception as exc:
            return {"success": False, "content": f"查询失败：{exc}"}
        return {
            "success": False,
            "content": f"不认识的 action：{action!r}，只能是 status / pool / orders / history / quote。",
        }

    @Tool(
        "sim_trader_review",
        description=(
            "让模拟盘此刻重新看一次盘并自行决定买不买、卖不卖。"
            "交易时段内会真实下单到模拟盘（不额外播报），闭市时只做决策不下单；"
            "聊天里任何人都能触发，同一聊天流有最短间隔限制"
        ),
        parameters=[
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="为什么现在想看盘，例如「群里有人怂恿加仓」；只作为参考，写不写都行",
                required=False,
            ),
        ],
    )
    async def tool_review(self, reason: str = "", **kwargs: Any) -> Dict[str, Any]:
        """主动看盘：交易时段内跑完整一轮（会真实下单），闭市时自动降级为只看不做。"""

        context = self._invocation_context(kwargs)
        stream_key = context["stream_id"] or "unknown"
        if self._busy:
            return {"success": False, "content": "已经有一轮在看盘了，等它跑完再说。"}
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        cooldown_left = self._manual_cooldown_left(stream_key)
        if cooldown_left > 0:
            return {"success": False, "content": f"刚跑过一轮，{int(cooldown_left) + 1} 秒后再来。"}
        # 闭市后允许聊天里问"你会怎么做"，但不能真下单：这一轮自动降级为试跑
        off_session = not self._in_trading_session(now)
        self._mark_manual_run(stream_key)
        if reason.strip():
            self.ctx.logger.info(
                "聊天里触发看盘，理由：%s，时段内=%s", reason.strip()[:80], not off_session
            )
        try:
            notes = await self._run_round(now, today, announce=False, dry_run=off_session)
        except Exception as exc:
            self.ctx.logger.error("聊天里触发看盘失败", exc_info=True)
            return {"success": False, "content": f"看盘失败了：{exc}"}
        state = self._require_state().load()
        summary = [f"心情：{state.last_mood or '平静'}", f"判断：{state.last_reasoning or '（没写）'}"]
        if off_session:
            summary.insert(0, "现在不在交易时段，这一轮只看不做，没有下单。")
        if notes:
            summary.extend(notes)
        else:
            summary.append("这轮什么都没做。")
        return {"success": True, "content": "\n".join(summary)}

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


def build_limits_block(
    *,
    limits: Dict[str, float],
    balance: Dict[str, Any],
    positions: Sequence[Holding],
    trades_used: int,
) -> List[str]:
    """把这一轮还剩的额度算成人话：单票上限与当前最重占比、总仓位余量、今天还能下几笔。

    仓位口径按「持仓市值 / 总资产」算，没扣未成交委托冻结的可用资金；
    真下单时 trading.resolve_quantity 还会用可用资金再压一次，不会超。
    让模型自己从「单票不超过 36%」推算「现在还能加多少」很容易算错，
    不如直接把数字摆出来，它只需要决定用不用。
    """

    single_cap = float(limits["max_position_ratio"])
    cash_floor = float(limits["min_cash_reserve_ratio"])
    trades_cap = int(limits["max_trades_per_day"])
    total_assets = _to_float(balance.get("totalAssets"))

    lines: List[str] = []
    if not positions:
        lines.append(f"- 单票上限 {single_cap:.0%}；现在空仓")
        lines.append(f"- 总仓位上限 {1 - cash_floor:.0%}（现金至少留 {cash_floor:.0%}）")
    elif total_assets <= 0:
        # 有持仓但总资产取不到：说清「占比这轮不算」，别让模型以为自己空仓
        lines.append(f"- 单票上限 {single_cap:.0%}；持有 {len(positions)} 只，但总资产取不到，占比这轮不算")
        lines.append(f"- 总仓位上限 {1 - cash_floor:.0%}（现金至少留 {cash_floor:.0%}）")
    else:
        heaviest = max(positions, key=lambda item: item.market_value)
        held_total = sum(item.market_value for item in positions)
        held_pct = held_total / total_assets
        lines.append(
            f"- 单票上限 {single_cap:.0%}；现在最重的一只 {heaviest.name}({heaviest.code}) "
            f"占 {heaviest.market_value / total_assets:.1%}"
        )
        lines.append(
            f"- 总仓位上限 {1 - cash_floor:.0%}（现金至少留 {cash_floor:.0%}），"
            f"现在 {held_pct:.1%}，还能再加 {max(0.0, 1 - cash_floor - held_pct):.1%}"
        )
    lines.append(
        f"- 今天已下 {trades_used} 笔，上限 {trades_cap} 笔，还能下 {max(0, trades_cap - trades_used)} 笔"
    )
    return lines


def create_plugin() -> SimTraderPlugin:
    """Runner 入口。"""

    return SimTraderPlugin()


__all__ = ["SimTraderConfig", "SimTraderPlugin", "create_plugin", "in_sessions", "parse_sessions"]
