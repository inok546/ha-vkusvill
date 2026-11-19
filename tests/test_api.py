"""Transport, order parsing, paging, rate limits, and status semantics."""

import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import pytest

from custom_components.vkusvill import models
from custom_components.vkusvill.api import MAX_PAGES, VkusVillClient, parse_page
from custom_components.vkusvill.const import CONF_TOKEN, MCP_URL, ORDER_TOOL
from custom_components.vkusvill.http import (
    AccessDenied,
    ConnectionError,
    Cooldown,
    HttpClient,
    ProtocolError,
    RateLimited,
    RpcError,
    ToolError,
    retry_after,
)
from custom_components.vkusvill.models import Order, OrderHistory, OrderStatus, Stage, select_orders
from custom_components.vkusvill.oauth import Credentials, Token

from .conftest import Response


@pytest.fixture
def client(wire, entry_data):
    token = Token(**entry_data[CONF_TOKEN])
    provider = AsyncMock()
    provider.get.return_value = token
    provider.credentials = Credentials(entry_data["client_id"], entry_data["client_secret"])
    return VkusVillClient(HttpClient(wire, Cooldown()), provider)


@pytest.mark.parametrize("wrapped", ("flat", "result", "text", "structured"))
def test_response_shapes(wrapped):
    items = [
        {
            "id_general": 12,
            "status_name": "Статус\u202e API",
            "products": [{"name": "Excluded from order model"}],
        }
    ]
    payload = {"data": {"orders": items, "meta": {"has_more": False}}}
    result = {
        "flat": payload,
        "result": {"id": 1, "result": payload},
        "text": {
            "id": 1,
            "result": {
                "content": [{"type": "image"}, {"text": "notice"}, {"text": json.dumps(payload)}]
            },
        },
        "structured": {"id": 1, "result": {"structuredContent": payload}},
    }[wrapped]
    parsed = parse_page(result, 1)
    assert parsed.items == (Order("12", "12", "Статус API", None),)
    assert parsed.has_more is False


@pytest.mark.parametrize(
    ("envelope", "error"),
    [
        ({"id": 1, "error": {"code": -32601, "message": "private"}}, RpcError),
        ({"id": 1, "result": {"isError": True}}, ToolError),
        (
            {"id": 1, "result": {"content": [{"text": '{"ok": false, "error": "forbidden"}'}]}},
            AccessDenied,
        ),
        ({"ok": False, "error": {"code": "database_failure"}}, ToolError),
        ({"data": {"items": "wrong type"}}, ProtocolError),
        ({"items": [], "meta": {"has_more": True}}, ProtocolError),
        ({"items": [{}], "meta": {"has_more": 1}}, ProtocolError),
        ({"items": [0]}, ProtocolError),
        ({"id": 2, "result": {"items": []}}, ProtocolError),
        ({"result": {"items": []}}, ProtocolError),
    ],
)
def test_protocol_failures(envelope, error):
    with pytest.raises(error, match="VkusVill") as raised:
        parse_page(envelope, 1)
    assert "private" not in str(raised.value)


async def test_page_requests_and_later_order(client, wire):
    wire.orders([{"id": 1, "created_at": 1000, "status_name": "unrecognized"}], has_more=True)
    wire.orders([{"id": 2, "created_at": 900, "status_name": "Сборка"}], page=2, has_more=False)
    result = await client.get_history()
    assert wire.pages == [1, 2]
    assert result.complete
    assert select_orders(result, datetime.now(UTC)).selected.number == "2"
    for _, url, request in wire.calls:
        assert url == MCP_URL
        rpc = request["json"]
        assert rpc["method"] == "tools/call"
        assert rpc["params"]["name"] == ORDER_TOOL


async def test_pagination_cap_and_dedupe(client, wire, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.vkusvill")
    for page in range(1, MAX_PAGES + 1):
        wire.orders([{"id": page}], page=page, has_more=True)
    result = await client.get_history()
    assert not result.complete
    assert len(result.orders) == MAX_PAGES
    wire.orders([{"id": 1, "status_name": "first"}], has_more=True)
    wire.orders([{"id": 1, "status_name": "duplicate"}], page=2, has_more=True)
    result = await client.get_history()
    assert not result.complete
    assert [order.status_name for order in result.orders] == ["first"]
    assert f"Order history stopped: page={MAX_PAGES}, reason=page_limit" in caplog.text
    assert "Order history stopped: page=2, reason=no_new_orders" in caplog.text
    assert "Orders page: page=2, received=1, retained=0, duplicates=1" in caplog.text


async def test_empty_history_ends_full_page_without_meta(client, wire):
    wire.orders([{"id": number} for number in range(10)])
    wire.orders([], page=2)
    result = await client.get_history()
    assert result.complete
    assert len(result.orders) == 10
    assert wire.pages == [1, 2]


async def test_sse_matches_rpc_id_and_stops_before_trailing_event(client, wire, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.vkusvill")
    wire.add(
        "POST",
        MCP_URL,
        Response(
            content_type="text/event-stream",
            raw=(
                b': keepalive\r\n\r\nevent: message\r\ndata: {"id":99}\r\n\r\n'
                b'event: message\r\ndata: {"id":1,\r\ndata: "result":{"items":[]}}\r\n\r\n'
                b"data: ignore\n\n"
            ),
        ),
    )
    result = await client.get_history()
    assert result == OrderHistory((), True)
    assert "HTTP response: operation=orders, status=200, sse=True" in caplog.text
    assert "data: ignore" not in caplog.text


async def test_timeout_and_malformed_json(client, wire, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.vkusvill")
    wire.add("POST", MCP_URL, TimeoutError("private timeout cause"))
    with pytest.raises(ConnectionError, match="Could not reach"):
        await client.get_history()
    wire.add("POST", MCP_URL, Response(raw=b'{"data"'))
    with pytest.raises(ProtocolError, match="Malformed"):
        await client.get_history()
    assert "HTTP request failed: operation=orders, reason=timeout" in caplog.text
    assert "private timeout cause" not in caplog.text


async def test_transport_size_cap_and_redirect(client, wire, monkeypatch):
    monkeypatch.setattr("custom_components.vkusvill.http.MCP_LIMIT", 20)
    wire.add("POST", MCP_URL, Response(raw=b"x" * 21))
    with pytest.raises(ProtocolError, match="size limit"):
        await client.get_history()
    wire.status(MCP_URL, 307, Location="https://example.invalid")
    with pytest.raises(ProtocolError, match="redirect"):
        await client.get_history()
    assert wire.pages == [1, 1]
    with pytest.raises(ProtocolError, match="bearer"):
        await client.http.request("POST", "https://oauth.vkusvill.ru/", bearer="x")
    assert len(wire.calls) == 2


@pytest.mark.parametrize("status", (429, 503))
async def test_server_cooldown(client, wire, monkeypatch, status):
    clock = 100.0
    monkeypatch.setattr("custom_components.vkusvill.http.monotonic", lambda: clock)
    wire.status(MCP_URL, status, **{"Retry-After": "75"})
    with pytest.raises(RateLimited) as raised:
        await client.get_history()
    assert raised.value.seconds == 75
    clock += 74
    with pytest.raises(RateLimited):
        await client.get_history()
    assert len(wire.calls) == 1
    clock += 2
    wire.orders([])
    assert (await client.get_history()).orders == ()
    assert len(wire.calls) == 2


def test_retry_after_formats():
    now = datetime(2026, 11, 18, tzinfo=UTC)
    assert retry_after("120", now) == 120
    assert retry_after(format_datetime(now + timedelta(seconds=120), usegmt=True), now) == 120
    assert retry_after("tomorrow", now) is None


def test_status_semantics(monkeypatch):
    now = datetime.now(UTC)
    past = now - timedelta(days=1)
    monkeypatch.setattr(
        models,
        "VERIFIED_STATUSES",
        {
            "fixture delivered": OrderStatus(Stage.DELIVERED, False),
            "fixture cancelled": OrderStatus(Stage.CANCELLED, False),
            "fixture assembling": OrderStatus(Stage.ASSEMBLING, True),
            "fixture courier": OrderStatus(Stage.WITH_COURIER, True),
        },
    )
    unknown = Order("5", "5", "Статус неизвестен", now)
    delivered = Order("4", "4", "fixture delivered", now)
    cancelled = Order("3", "3", "fixture cancelled", past)
    older = Order("1", "1", "fixture assembling", past)
    selected = Order("2", "2", "fixture courier", past)
    snapshot = select_orders(
        OrderHistory((unknown, delivered, selected, cancelled, older), True), now
    )
    assert snapshot.selected is selected
    assert snapshot.has_active_order is True
    assert snapshot.selected.stage == Stage.WITH_COURIER
    terminal = select_orders(OrderHistory((delivered, cancelled), True), now)
    assert terminal.selected.stage == Stage.DELIVERED
    assert cancelled.stage == Stage.CANCELLED
    assert terminal.has_active_order is False
    assert select_orders(OrderHistory((delivered,), False), now).has_active_order is None
    assert select_orders(OrderHistory((unknown,), True), now).has_active_order is None
    assert select_orders(OrderHistory((), True), now).has_active_order is None


def test_production_mapping_does_not_guess():
    for raw in ("Доставлен", "Отменён", "У курьера", "Собирается", "Успешно завершён", None):
        order = Order("1", "1", raw, None)
        assert order.stage == Stage.UNKNOWN
        assert order.status.active is None


@pytest.mark.parametrize(
    ("service", "expected"),
    [("Доставка", Stage.DELIVERED), ("Самовывоз", Stage.OTHER), (None, Stage.OTHER)],
)
def test_completed_status_uses_provider_service(service, expected):
    page = parse_page({"items": [{"status_name": "Успешно завершен", "service": service}]}, 1)
    assert page.items[0].stage == expected
    assert page.items[0].status.active is False


@pytest.mark.parametrize(
    "timestamp", [1795010400, 1795010400000, "1795010400000", "2026-11-18T14:00:00+00:00"]
)
def test_order_creation_time_contract(timestamp):
    page = parse_page({"items": [{"created_at": timestamp}]}, 1)
    assert page.items[0].created_at == datetime(2026, 11, 18, 14, tzinfo=UTC)


def test_flagged_tool_error_uses_inner_permission_code():
    envelope = {
        "id": 1,
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"ok": False, "error": {"code": "insufficient_scope"}}),
                }
            ],
        },
    }
    with pytest.raises(AccessDenied, match="denied access"):
        parse_page(envelope, 1)
