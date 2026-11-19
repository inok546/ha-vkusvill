"""Scripted HTTP responses used by the transport and Home Assistant tests."""

import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any

import pytest
from multidict import CIMultiDict

from custom_components.vkusvill.const import (
    CONF_TOKEN,
    DOMAIN,
    MCP_URL,
    OAUTH_ORIGIN,
    REQUESTED_SCOPE,
)
from custom_components.vkusvill.oauth import (
    AUTHORIZE_URL,
    METADATA_URL,
    RESOURCE_URL,
    TOKEN_URL,
    Token,
)


@dataclass
class Response:
    """Minimal aiohttp response interface for chunked reads."""

    status: int = 200
    raw: bytes = b""
    content_type: str = "application/json"
    headers: CIMultiDict = field(default_factory=CIMultiDict)

    @property
    def content(self) -> Response:
        return self

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.raw), min(size, 17)):
            yield self.raw[offset : offset + min(size, 17)]


class Wire:
    """Record requests, and serve queued responses without external access."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], deque] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def add(self, method: str, url: str, response: Any) -> None:
        self.responses.setdefault((method, url), deque()).append(response)

    def json(self, method: str, url: str, payload: Any, status: int = 200) -> None:
        self.add(method, url, Response(status=status, raw=json.dumps(payload).encode()))

    def discovery(self) -> None:
        self.json(
            "GET",
            f"{RESOURCE_URL}/mcp",
            {
                "resource": MCP_URL,
                "authorization_servers": [f"{OAUTH_ORIGIN}/"],
                "scopes_supported": ["purchase_history.read"],
            },
        )
        self.metadata()

    def metadata(self, **overrides: Any) -> None:
        self.json(
            "GET",
            METADATA_URL,
            {
                "issuer": f"{OAUTH_ORIGIN}/",
                "authorization_endpoint": AUTHORIZE_URL,
                "token_endpoint": TOKEN_URL,
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                **overrides,
            },
        )

    def orders(self, items: list, page: int = 1, **meta: Any) -> None:
        self.json(
            "POST",
            MCP_URL,
            {
                "jsonrpc": "2.0",
                "id": page,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"data": {"items": items, "meta": meta}}),
                        }
                    ]
                },
            },
        )

    @property
    def pages(self) -> list[int]:
        return [
            request["json"]["params"]["arguments"]["page"]
            for _, url, request in self.calls
            if url == MCP_URL
        ]

    def status(self, url: str, status: int, **headers: str) -> None:
        self.add("POST", url, Response(status=status, headers=CIMultiDict(headers)))

    def request(self, method: str, url: str, **kwargs: Any) -> WireRequest:
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"].total == 20
        queue = self.responses.get((method, url))
        assert queue, f"Unexpected HTTP request to {method} {url}"
        return WireRequest(queue.popleft(), kwargs)


class WireRequest:
    """An async request context that can simulate gated responses."""

    def __init__(self, response: Any, kwargs: dict[str, Any]) -> None:
        self.response = response
        self.kwargs = kwargs

    async def __aenter__(self) -> Response:
        if isinstance(self.response, Exception):
            raise self.response
        if callable(self.response):
            return await self.response(self.kwargs)
        return self.response

    async def __aexit__(self, *exc: object) -> None:
        pass


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Replace only the session used by the integration."""
    instance = Wire()
    monkeypatch.setattr("custom_components.vkusvill.async_get_clientsession", lambda _: instance)
    return instance


def _test_grant() -> dict[str, Any]:
    return {
        "access_token": token_urlsafe(24),
        "refresh_token": token_urlsafe(24),
        "expires_in": 86400,
        "scope": REQUESTED_SCOPE,
        "token_type": "Bearer",
    }


@pytest.fixture
def oauth_response() -> dict[str, Any]:
    """Generate a non-reusable test grant entirely in memory."""
    return _test_grant()


@pytest.fixture
def entry_data() -> dict[str, Any]:
    """Generate Home Assistant config entry data."""
    from dataclasses import asdict

    return {
        "client_id": "test-application",
        "client_secret": token_urlsafe(24),
        CONF_TOKEN: asdict(Token.from_response(_test_grant(), REQUESTED_SCOPE)),
    }


@pytest.fixture
def entry(hass, entry_data):
    """Register an unloaded entry in Home Assistant."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    config_entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, data=entry_data)
    config_entry.add_to_hass(hass)
    return config_entry
