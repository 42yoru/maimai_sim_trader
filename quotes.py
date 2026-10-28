"""腾讯行情快照：盘中实时报价。

妙想的 query 是自然语言接口，适合做深度分析但有额度成本；
盘中每轮都要看二十来只票的最新价，用腾讯的批量快照更快也更省。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import httpx
import logging

SNAPSHOT_URL = "https://qt.gtimg.cn/q="
# 腾讯返回 GBK，且一次最多几十只比较稳
MAX_BATCH = 40

logger = logging.getLogger("maimai_sim_trader.quotes")


class QuoteError(RuntimeError):
    """行情接口返回了无法解析的内容。"""


@dataclass(frozen=True)
class Quote:
    """一只票的实时快照。"""

    code: str
    name: str
    price: float
    prev_close: float
    open_price: float
    high: float
    low: float
    change: float
    change_pct: float
    volume_lots: float
    turnover_wan: float
    turnover_rate: float
    pe: float
    updated_at: str
    # 停牌票：现价为 0，price 只是用昨收顶上来的参考值，不能拿去下单
    suspended: bool = False

    def to_brief(self) -> str:
        """拼成一行给模型看的摘要。"""

        return (
            f"{self.code} {self.name} {self.price:.2f}元 涨跌{self.change_pct:+.2f}% "
            f"最高{self.high:.2f} 最低{self.low:.2f} 换手{self.turnover_rate:.2f}%"
        )

def to_market_symbol(code: str) -> str:
    """六位代码转腾讯前缀：6/9 开头走沪市，其余走深市。"""

    normalized = code.strip()
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError(f"股票代码必须是六位数字，收到 {code!r}")
    return f"sh{normalized}" if normalized.startswith(("6", "9")) else f"sz{normalized}"


def _to_float(value: str, *, field: str, code: str) -> float:
    """安全取浮点；拿不到就说明行情的字段位置变了，直接报错。"""

    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise QuoteError(f"解析 {code} 的 {field} 失败: {value!r}") from exc


def parse_snapshot(text: str) -> Dict[str, Quote]:
    """解析腾讯快照报文。

    单只票解析失败（字段数异常、数值格式变化）只跳过并留痕：一只坏票不该拖垮整个股票池。
    但整批都解析不出来时说明字段结构真的变了，照样抛错。
    """

    quotes: Dict[str, Quote] = {}
    seen = 0
    failures: List[str] = []
    for line in text.split(";"):
        raw = line.strip()
        if not raw.startswith("v_"):
            continue
        if raw.startswith("v_pv_none_match"):
            # 腾讯对「查不到的代码」单独回这一行：同样以 v_ 开头、字段很少，但这是这一只票
            # 没数据，不是整批请求失败（股票池是模型可以自己改的，一条幻觉代码不该让全天停摆）
            logger.warning("腾讯快照查不到 %s 的数据（代码不存在或已退市），已跳过", raw.partition("=")[2].strip())
            continue
        seen += 1
        try:
            quote = _parse_snapshot_line(raw)
        except QuoteError as exc:
            logger.warning("跳过一只行情: %s", exc)
            failures.append(str(exc))
            continue
        quotes[quote.code] = quote
    if seen and not quotes:
        raise QuoteError("整批行情都无法解析，字段结构可能变了: " + "; ".join(failures[:3]))
    return quotes


def _parse_snapshot_line(raw: str) -> Quote:
    """解析单行腾讯快照。"""

    header, _, body = raw.partition("=")
    symbol = header[2:].strip()
    fields = body.strip().strip('"').split("~")
    if len(fields) < 40:
        raise QuoteError(f"{symbol} 的字段数量异常：{len(fields)}")
    code = fields[2].strip()
    price = _to_float(fields[3], field="现价", code=code)
    suspended = price <= 0
    if suspended:
        # 停牌时现价为 0，用昨收顶上来避免后续除零；但标记出来，下单前会被跳过
        price = _to_float(fields[4], field="昨收", code=code)
    return Quote(
        code=code,
        name="".join(fields[1].split()),
        price=price,
        prev_close=_to_float(fields[4], field="昨收", code=code),
        open_price=_to_float(fields[5], field="今开", code=code),
        high=_to_float(fields[33], field="最高", code=code),
        low=_to_float(fields[34], field="最低", code=code),
        change=_to_float(fields[31], field="涨跌", code=code),
        change_pct=_to_float(fields[32], field="涨跌幅", code=code),
        volume_lots=_to_float(fields[6], field="成交量", code=code),
        turnover_wan=_to_float(fields[37], field="成交额", code=code),
        turnover_rate=_to_float(fields[38], field="换手率", code=code),
        pe=_to_float(fields[39], field="市盈率", code=code),
        updated_at=fields[30].strip(),
        suspended=suspended,
    )


class TencentQuoteClient:
    """腾讯快照客户端，支持一次拉多只。"""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建连接池。"""

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        return self._client

    async def close(self) -> None:
        """释放连接池。"""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, codes: Iterable[str]) -> Dict[str, Quote]:
        """批量拉取快照，按代码返回。"""

        symbols: List[str] = []
        for code in codes:
            symbol = to_market_symbol(code)
            if symbol not in symbols:
                symbols.append(symbol)
        if not symbols:
            return {}
        client = await self._ensure_client()
        result: Dict[str, Quote] = {}
        for start in range(0, len(symbols), MAX_BATCH):
            batch = symbols[start : start + MAX_BATCH]
            response = await client.get(f"{SNAPSHOT_URL}{','.join(batch)}")
            response.raise_for_status()
            text = response.content.decode("gbk", errors="replace")
            result.update(parse_snapshot(text))
        return result


__all__ = ["MAX_BATCH", "Quote", "QuoteError", "SNAPSHOT_URL", "TencentQuoteClient", "parse_snapshot", "to_market_symbol"]
