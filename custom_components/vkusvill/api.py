"""Read and parse order history through the official MCP tool."""

import asyncio
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from .const import MCP_ORIGIN, MCP_URL, ORDER_TOOL
from .http import (
    AccessDenied,
    HttpClient,
    ProtocolError,
    RpcError,
    ToolError,
    Unauthorized,
    decode_object,
)
from .models import Order, OrderHistory, OrderPage
from .oauth import Token, TokenManager

PAGE_SIZE = 10
MAX_PAGES = 8
POLL_TIMEOUT = 60
_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("Unexpected VkusVill order history structure")
    return value


def _reject_failure(value: dict[str, Any], error_type: type[RpcError | ToolError]) -> None:
    error = value.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    if code in (403, "forbidden", "insufficient_scope", "permission_denied"):
        raise AccessDenied("VkusVill denied access to order history")
    if code in (401, "unauthorized", "invalid_token"):
        raise Unauthorized("VkusVill rejected the access token")
    raise error_type("VkusVill reported an order history error")


def _check_tool(value: dict[str, Any]) -> None:
    if value.get("isError") is True or value.get("ok") is False or value.get("error"):
        _reject_failure(value, ToolError)


def _payload(envelope: dict[str, Any], rpc_id: int) -> dict[str, Any]:
    if "jsonrpc" in envelope or "result" in envelope or {"id", "error"} <= envelope.keys():
        if envelope.get("jsonrpc", "2.0") != "2.0":
            raise ProtocolError("Unsupported VkusVill JSON-RPC version")
        if envelope.get("id") != rpc_id and not (
            envelope.get("id") is None and "error" in envelope
        ):
            raise ProtocolError("VkusVill JSON-RPC response ID did not match")
        if "error" in envelope:
            _reject_failure(envelope, RpcError)
        result = _dict(envelope.get("result"))
    else:
        result = envelope

    if result.get("ok") is False or result.get("error"):
        _reject_failure(result, ToolError)
    if "structuredContent" in result:
        inner = _dict(result["structuredContent"])
    elif "content" in result:
        content = result["content"]
        if not isinstance(content, list):
            raise ProtocolError("Unexpected VkusVill MCP content")
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type", "text") == "text"
                and isinstance((value := block.get("text")), str)
            ):
                try:
                    inner = decode_object(value)
                except ProtocolError:
                    continue
                _check_tool(inner)
                if any(key in inner for key in ("data", "items", "orders")):
                    break
        else:
            _check_tool(result)
            raise ProtocolError("VkusVill MCP response contained no order data")
    else:
        inner = result
    _check_tool(inner)
    _check_tool(result)
    return inner


def _identifier(value: Any, sensitive: tuple[str, ...]) -> str | None:
    if type(value) is int and 0 <= value < 10**64:
        value = str(value)
    if isinstance(value, str) and _ORDER_ID.fullmatch(value):
        if not any(secret in value for secret in sensitive):
            return value
    return None


def _status(value: Any, sensitive: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 255:
        return None
    clean = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    return clean if clean.strip() and not any(secret in clean for secret in sensitive) else None


def _created_at(value: Any) -> datetime | None:
    try:
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
            value = int(value)
        if type(value) in (int, float):
            seconds = value / 1000 if value >= 10**12 else value
            return datetime.fromtimestamp(seconds, UTC)
        if isinstance(value, str) and 1 <= len(value) <= 40:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError, OverflowError, OSError:
        pass
    return None


def parse_page(envelope: dict[str, Any], rpc_id: int, sensitive: tuple[str, ...] = ()) -> OrderPage:
    """Extract a page without retaining product lines or the MCP envelope."""
    inner = _payload(envelope, rpc_id)
    data = _dict(inner.get("data", inner))
    _check_tool(data)
    items = data.get("items", data.get("orders"))
    if not isinstance(items, list):
        raise ProtocolError("VkusVill order history did not contain an order list")
    meta = _dict(data.get("meta", inner.get("meta", {})))
    has_more = meta.get("has_more")
    if "has_more" in meta and type(has_more) is not bool:
        raise ProtocolError("Invalid VkusVill pagination marker")
    if not items and has_more is True:
        raise ProtocolError("VkusVill returned an empty page with more orders")
    orders = []
    for value in items:
        item = _dict(value)
        order_id = _identifier(item.get("id"), sensitive)
        number = _identifier(item.get("id_general"), sensitive)
        status = _status(item.get("status_name"), sensitive)
        created_at = _created_at(item.get("created_at"))
        orders.append(
            Order(
                order_id or number,
                number or order_id,
                status,
                created_at,
                is_delivery=item.get("service") == "Доставка",
            )
        )
    return OrderPage(tuple(orders), has_more)


async def fetch_order_page(
    http: HttpClient, token: Token, page: int, client_secret: str
) -> OrderPage:
    """Call only the read-only orders history tool on the exact MCP endpoint."""
    status, response = await http.request(
        "POST",
        MCP_URL,
        bearer=token.access_token,
        rpc={
            "jsonrpc": "2.0",
            "id": page,
            "method": "tools/call",
            "params": {"name": ORDER_TOOL, "arguments": {"page": page}},
        },
    )
    if status != 200:
        raise ProtocolError(f"VkusVill MCP returned HTTP {status}")
    return parse_page(response, page, (token.access_token, token.refresh_token, client_secret))


class VkusVillClient:
    """Fetch bounded snapshots for one config entry."""

    def __init__(self, http: HttpClient, tokens: TokenManager) -> None:
        self.http = http
        self.tokens = tokens
        self._poll_lock = asyncio.Lock()
        self._closed = False

    async def close(self) -> None:
        """Wait for pending requests and prevent further traffic on unload."""
        self._closed = True
        async with self._poll_lock:
            await self.tokens.close()

    async def _page(self, page: int) -> OrderPage:
        self.http.cooldown.check(MCP_ORIGIN)
        token = await self.tokens.get()
        secret = self.tokens.credentials.client_secret
        try:
            return await fetch_order_page(self.http, token, page, secret)
        except Unauthorized:
            rotated = await self.tokens.get(rejected=token)
            return await fetch_order_page(self.http, rotated, page, secret)

    async def get_history(self) -> OrderHistory:
        """Read pages in order, stopping at the advertised end or a defensive cap."""
        async with self._poll_lock, asyncio.timeout(POLL_TIMEOUT):
            if self._closed:
                raise ProtocolError("VkusVill client is closed")
            orders: list[Order] = []
            seen: set[str] = set()
            duplicate = False
            for page in range(1, MAX_PAGES + 1):
                result = await self._page(page)
                added = False
                for order in result.items:
                    if order.key is not None:
                        if order.key in seen:
                            duplicate = True
                            continue
                        seen.add(order.key)
                    orders.append(order)
                    added = True
                if result.has_more is False or (
                    result.has_more is None and len(result.items) < PAGE_SIZE
                ):
                    return OrderHistory(tuple(orders), not duplicate)
                if not added:
                    break
            return OrderHistory(tuple(orders), False)
