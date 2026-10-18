"""东方财富妙想 HTTP 客户端。

只做一件事：把妙想的 REST 接口包装成带类型的方法，并把业务错误显式抛出来。
不在这里做任何兜底或降级——接口出错就让调用方看到真实的 code / message。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import logging

import httpx

DEFAULT_BASE_URL = "https://mkapi2.dfcfs.com/finskillshub"

BALANCE_ENDPOINT = "/api/claw/mockTrading/balance"
POSITIONS_ENDPOINT = "/api/claw/mockTrading/positions"
ORDERS_ENDPOINT = "/api/claw/mockTrading/orders"
TRADE_ENDPOINT = "/api/claw/mockTrading/trade"
CANCEL_ENDPOINT = "/api/claw/mockTrading/cancel"
QUERY_ENDPOINT = "/api/claw/query"
NEWS_ENDPOINT = "/api/claw/news-search"
SCREEN_ENDPOINT = "/api/claw/stock-screen"

SUCCESS_CODES = frozenset({"0", "200"})

DIRECTIONS = ("buy", "sell")


class MxApiError(RuntimeError):
    """妙想接口返回的业务错误（HTTP 200 但 code 非成功）。"""

    def __init__(self, message: str, *, code: str = "", endpoint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint


def ensure_success(payload: Dict[str, Any], *, endpoint: str) -> None:
    """按妙想的两种报文风格判定成功与否，失败直接抛错。"""

    code = payload.get("code")
    if code is not None:
        if str(code) in SUCCESS_CODES:
            return
        message = str(payload.get("message") or payload.get("msg") or f"妙想接口返回 code={code}")
        raise MxApiError(message, code=str(code), endpoint=endpoint)
    if payload.get("success") is True:
        return
    message = str(payload.get("message") or payload.get("msg") or "妙想接口返回了无法识别的报文")
    raise MxApiError(message, endpoint=endpoint)


def _as_dict(value: Any, *, endpoint: str) -> Dict[str, Any]:
    """把 data 字段规整成字典；类型不符说明协议变了，直接报错。"""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MxApiError(f"妙想接口 {endpoint} 的 data 不是对象: {type(value).__name__}", endpoint=endpoint)
    return value


class MxClient:
    """妙想 API 异步客户端。

    一个实例对应一把 key，连接复用；用完记得 ``await close()``。
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self._timeout = timeout
        self._logger = logger or logging.getLogger(__name__)
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def base_url(self) -> str:
        """当前使用的妙想服务地址。"""

        return self._base_url

    async def _ensure_client(self) -> httpx.AsyncClient:
        """惰性创建连接池。"""

        if self._client is None:
            # trust_env=False：本机曾出现畸形代理变量导致 httpx 建 URL 时直接崩
            self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        return self._client

    async def close(self) -> None:
        """释放连接池。"""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, endpoint: str, body: Dict[str, Any]) -> Any:
        """发一次 POST，校验业务码后返回 data。"""

        if not self._api_key:
            raise MxApiError("未配置妙想 API Key", endpoint=endpoint)
        client = await self._ensure_client()
        response = await client.post(
            f"{self._base_url}{endpoint}",
            json=body,
            headers={"apikey": self._api_key, "Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise MxApiError("妙想返回的报文不是 JSON 对象", endpoint=endpoint)
        ensure_success(payload, endpoint=endpoint)
        return payload.get("data")

    # ──── 账户 ────

    async def get_balance(self) -> Dict[str, Any]:
        """读取模拟组合资金与净值。"""

        data = await self._post(BALANCE_ENDPOINT, {"moneyUnit": 1})
        return _as_dict(data, endpoint=BALANCE_ENDPOINT)

    async def get_positions(self) -> List[Dict[str, Any]]:
        """读取模拟组合持仓明细。"""

        data = _as_dict(await self._post(POSITIONS_ENDPOINT, {"moneyUnit": 1}), endpoint=POSITIONS_ENDPOINT)
        rows = data.get("posList") or []
        if not isinstance(rows, list):
            raise MxApiError("妙想持仓 posList 不是列表", endpoint=POSITIONS_ENDPOINT)
        return [row for row in rows if isinstance(row, dict)]

    async def get_orders(self) -> List[Dict[str, Any]]:
        """读取当日委托。"""

        data = _as_dict(
            await self._post(ORDERS_ENDPOINT, {"fltOrderDrt": 0, "fltOrderStatus": 0}),
            endpoint=ORDERS_ENDPOINT,
        )
        rows = data.get("orders") or []
        if not isinstance(rows, list):
            raise MxApiError("妙想委托 orders 不是列表", endpoint=ORDERS_ENDPOINT)
        return [row for row in rows if isinstance(row, dict)]

    # ──── 交易 ────

    async def place_order(
        self,
        *,
        direction: str,
        stock_code: str,
        price: float,
        quantity: int,
    ) -> Dict[str, Any]:
        """下一条限价委托。妙想不支持市价单，useMarketPrice 恒为 False。"""

        if direction not in DIRECTIONS:
            raise ValueError(f"direction 只能是 {'/'.join(DIRECTIONS)}，收到 {direction!r}")
        if quantity <= 0 or quantity % 100 != 0:
            raise ValueError(f"委托数量必须是 100 的正整数倍，收到 {quantity}")
        if price <= 0:
            raise ValueError(f"委托价格必须大于 0，收到 {price}")
        body = {
            "type": direction,
            "stockCode": stock_code,
            "price": round(price, 3),
            "quantity": quantity,
            "useMarketPrice": False,
        }
        data = await self._post(TRADE_ENDPOINT, body)
        return _as_dict(data, endpoint=TRADE_ENDPOINT)

    async def cancel_order(self, *, order_id: str, stock_code: str) -> Dict[str, Any]:
        """撤销一条指定委托。"""

        body = {"type": "order", "orderId": order_id, "stockCode": stock_code}
        data = await self._post(CANCEL_ENDPOINT, body)
        return _as_dict(data, endpoint=CANCEL_ENDPOINT)

    async def cancel_all(self) -> Dict[str, Any]:
        """撤销全部未成交委托。"""

        data = await self._post(CANCEL_ENDPOINT, {"type": "all"})
        return _as_dict(data, endpoint=CANCEL_ENDPOINT)

    # ──── 数据 ────

    async def query(self, tool_query: str) -> Dict[str, Any]:
        """自然语言金融数据查询（行情、财务、资金流等）。"""

        text = tool_query.strip()
        if not text:
            raise ValueError("查询内容不能为空")
        data = await self._post(QUERY_ENDPOINT, {"toolQuery": text})
        return _as_dict(data, endpoint=QUERY_ENDPOINT)

    async def search_news(self, query: str) -> Dict[str, Any]:
        """资讯搜索。"""

        text = query.strip()
        if not text:
            raise ValueError("搜索内容不能为空")
        data = await self._post(NEWS_ENDPOINT, {"query": text})
        return _as_dict(data, endpoint=NEWS_ENDPOINT)

    async def screen(self, keyword: str) -> Dict[str, Any]:
        """条件选股。"""

        text = keyword.strip()
        if not text:
            raise ValueError("选股条件不能为空")
        data = await self._post(SCREEN_ENDPOINT, {"keyword": text})
        return _as_dict(data, endpoint=SCREEN_ENDPOINT)


__all__ = [
    "DEFAULT_BASE_URL",
    "DIRECTIONS",
    "MxApiError",
    "MxClient",
    "ensure_success",
]
