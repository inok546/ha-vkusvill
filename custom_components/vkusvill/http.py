"""Bounded HTTP transport shared by OAuth and MCP."""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

from .const import MCP_ORIGIN, OAUTH_ORIGIN

TIMEOUT = ClientTimeout(total=20, connect=8, sock_read=12)
MCP_LIMIT = 4 * 1024 * 1024
OAUTH_LIMIT = 256 * 1024


class VkusVillError(Exception):
    """An error with a safe, integration-defined message."""


class ConnectionError(VkusVillError):
    """Failed to communicate with the service."""


class ProtocolError(VkusVillError):
    """An unexpected response or endpoint."""


class RpcError(VkusVillError):
    """JSON-RPC error envelope."""


class ToolError(VkusVillError):
    """An unsuccessful MCP tool execution."""


class AuthError(VkusVillError):
    """The authorization must be renewed."""


class AccessDenied(AuthError):
    """The grant lacks required permissions."""


class Unauthorized(AuthError):
    """The access token was rejected."""


class RateLimited(VkusVillError):
    """The service asked the client to slow down."""

    def __init__(self, seconds: float, *, status: int | None = None) -> None:
        super().__init__("VkusVill asked to delay requests")
        self.seconds = seconds
        self.status = status


def retry_after(value: str | None, now: datetime) -> float | None:
    """Read delay-seconds or an HTTP date."""
    if not value or len(value) > 100:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        target = parsedate_to_datetime(value)
        return (target.replace(tzinfo=target.tzinfo or UTC) - now).total_seconds()
    except ValueError, OverflowError, TypeError:
        return None


@dataclass
class Cooldown:
    """Track server-requested pauses across requests and entry reloads."""

    deadlines: dict[str, float] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)

    def check(self, origin: str) -> None:
        """Skip requests scheduled before the deadline."""
        remaining = self.deadlines.get(origin, 0) - monotonic()
        if remaining > 0:
            raise RateLimited(remaining)

    def limited(self, origin: str, status: int, header: str | None) -> RateLimited:
        """Store the provider delay or bounded exponential backoff."""
        failures = min(self.failures.get(origin, 0) + 1, 5)
        self.failures[origin] = failures
        seconds = retry_after(header, datetime.now(UTC))
        seconds = max(60, seconds) if seconds is not None else 60 * 2 ** (failures - 1)
        deadline = max(self.deadlines.get(origin, 0), monotonic() + seconds)
        self.deadlines[origin] = deadline
        return RateLimited(deadline - monotonic(), status=status)

    def success(self, origin: str) -> None:
        """Reset backoff after success."""
        if self.deadlines.get(origin, 0) <= monotonic():
            self.deadlines.pop(origin, None)
            self.failures.pop(origin, None)


def decode_object(raw: bytes | str) -> dict[str, Any]:
    """Decode a JSON object without leaking input on failure."""
    try:
        value = json.loads(raw)
    except ValueError, RecursionError:
        raise ProtocolError("Malformed VkusVill JSON response") from None
    if not isinstance(value, dict):
        raise ProtocolError("Expected a VkusVill JSON object")
    return value


async def _body(response: ClientResponse, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.content.iter_chunked(16 * 1024):
        body.extend(chunk)
        if len(body) > limit:
            raise ProtocolError("VkusVill response exceeded its size limit")
    return bytes(body)


async def _events(response: ClientResponse, rpc_id: int) -> dict[str, Any]:
    total = 0
    partial = bytearray()
    data: list[bytes] = []
    async for chunk in response.content.iter_chunked(16 * 1024):
        total += len(chunk)
        if total > MCP_LIMIT:
            raise ProtocolError("VkusVill stream exceeded its size limit")
        lines = chunk.split(b"\n")
        for line in lines[:-1]:
            partial.extend(line)
            complete = bytes(partial).rstrip(b"\r")
            partial.clear()
            if complete.startswith((b"data:", b"\xef\xbb\xbfdata:")):
                data.append(complete.split(b":", 1)[1].removeprefix(b" "))
            elif not complete and data:
                message = decode_object(b"\n".join(data))
                data.clear()
                if message.get("id") == rpc_id or (
                    "error" in message and message.get("id") is None
                ):
                    return message
        partial.extend(lines[-1])
    raise ProtocolError("VkusVill stream ended before the JSON-RPC response")


class HttpClient:
    """Send requests to fixed official origins without following redirects."""

    def __init__(self, session: ClientSession, cooldown: Cooldown) -> None:
        self.session = session
        self.cooldown = cooldown

    async def request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        rpc: dict[str, Any] | None = None,
        bearer: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Read a JSON response or the matching JSON-RPC event."""
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in (MCP_ORIGIN, OAUTH_ORIGIN):
            raise ProtocolError("Unexpected VkusVill request origin")
        if bearer is not None and url != f"{MCP_ORIGIN}/mcp":
            raise ProtocolError("Unexpected bearer token destination")
        if form is not None and url != f"{OAUTH_ORIGIN}/oauth2/token":
            raise ProtocolError("Unexpected OAuth credentials destination")

        self.cooldown.check(origin)
        headers = {"Accept": "application/json, text/event-stream" if rpc else "application/json"}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        try:
            async with self.session.request(
                method,
                url,
                headers=headers,
                json=rpc,
                data=form,
                timeout=TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                status = response.status
                if 300 <= status < 400:
                    raise ProtocolError("VkusVill returned an unexpected redirect")
                if status in (429, 503):
                    raise self.cooldown.limited(origin, status, response.headers.get("Retry-After"))
                if rpc is not None and (
                    status == 403
                    or "insufficient_scope" in response.headers.get("WWW-Authenticate", "").lower()
                ):
                    raise AccessDenied("VkusVill denied access to order history")
                if rpc is not None and status == 401:
                    raise Unauthorized("VkusVill rejected the access token")
                if status >= 400 and (form is None or status >= 500):
                    return status, {}
                if rpc is not None and response.content_type == "text/event-stream":
                    value = await _events(response, rpc["id"])
                else:
                    body = await _body(response, MCP_LIMIT if rpc else OAUTH_LIMIT)
                    value = decode_object(body)
                if status < 400:
                    self.cooldown.success(origin)
                return status, value
        except ClientError, TimeoutError:
            raise ConnectionError("Could not reach VkusVill") from None
