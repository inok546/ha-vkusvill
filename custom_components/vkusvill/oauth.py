"""VkusVill OAuth discovery, manual callback, and serialized refresh."""

import asyncio
import re
from base64 import urlsafe_b64encode
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from time import monotonic, time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_TOKEN,
    DOMAIN,
    MANUAL_REDIRECT_URI,
    MCP_ORIGIN,
    MCP_URL,
    OAUTH_ORIGIN,
    ORDERS_SCOPE,
    REQUESTED_SCOPE,
)
from .http import AccessDenied, AuthError, ConnectionError, HttpClient, ProtocolError, RateLimited

AUTHORIZE_URL = f"{OAUTH_ORIGIN}/oauth2/auth"
TOKEN_URL = f"{OAUTH_ORIGIN}/oauth2/token"
METADATA_URL = f"{OAUTH_ORIGIN}/.well-known/oauth-authorization-server"
RESOURCE_URL = f"{MCP_ORIGIN}/.well-known/oauth-protected-resource"
FLOW_TTL = 300
TOKEN_EARLY = 120
TOKEN_VALUE = re.compile(r"[!-~]{1,8192}")


@dataclass(frozen=True, repr=False)
class Credentials:
    """Registered personal VkusVill OAuth application."""

    client_id: str
    client_secret: str


@dataclass(frozen=True, repr=False)
class Token:
    """Only the fields required to use, rotate and track a grant."""

    access_token: str
    refresh_token: str
    expires_at: float
    refresh_after: float
    scope: str
    grant_id: str
    rotation: int = 0

    @classmethod
    def from_response(cls, value: dict[str, Any], scope: str) -> Token:
        """Validate a token response before committing the new token pair."""
        access = value.get("access_token")
        refresh = value.get("refresh_token")
        expires = value.get("expires_in")
        granted = value.get("scope", scope)
        token_type = value.get("token_type", "Bearer")
        if not isinstance(granted, str) or ORDERS_SCOPE not in granted.split():
            raise AccessDenied("VkusVill did not grant access to order history")
        if not isinstance(refresh, str) or not TOKEN_VALUE.fullmatch(refresh):
            raise AuthError("VkusVill did not return a usable refresh token")
        if (
            not isinstance(access, str)
            or not TOKEN_VALUE.fullmatch(access)
            or type(expires) not in (int, float)
            or not 0 < expires <= 365 * 86400
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
        ):
            raise ProtocolError("Invalid VkusVill OAuth token response")
        now = time()
        return cls(
            access,
            refresh,
            now + expires,
            now + expires - min(TOKEN_EARLY, expires / 2),
            " ".join(granted.split()),
            uuid4().hex,
        )


class CallbackError(AuthError):
    """A safe config-flow error code for the manual callback."""


@dataclass(frozen=True, repr=False)
class AuthorizationAttempt:
    """Short-lived PKCE and CSRF material held only by the flow."""

    state: str
    verifier: str
    deadline: float

    @classmethod
    def create(cls) -> AuthorizationAttempt:
        """Generate fresh PKCE and state values."""
        return cls(token_urlsafe(24), token_urlsafe(48), monotonic() + FLOW_TTL)

    def url(self, credentials: Credentials) -> str:
        """Build the official authorization URL with PKCE S256."""
        challenge = urlsafe_b64encode(sha256(self.verifier.encode("ascii")).digest())
        query = urlencode(
            {
                "client_id": credentials.client_id,
                "redirect_uri": MANUAL_REDIRECT_URI,
                "response_type": "code",
                "scope": REQUESTED_SCOPE,
                "state": self.state,
                "code_challenge": challenge.decode("ascii").rstrip("="),
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def code_from_callback(self, callback: str) -> str:
        """Extract a single code from the exact loopback URL, without visiting it."""
        if monotonic() >= self.deadline:
            raise CallbackError("expired")
        callback = callback.strip()
        if len(callback) > 8192 or not callback.startswith(f"{MANUAL_REDIRECT_URI}?"):
            raise CallbackError("invalid_callback")
        try:
            parsed = urlsplit(callback)
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
        except ValueError:
            raise CallbackError("invalid_callback") from None
        states = query.get("state", [])
        if (
            "#" in callback
            or len(states) != 1
            or not states[0].isascii()
            or not compare_digest(states[0], self.state)
        ):
            raise CallbackError("invalid_callback")
        errors = query.get("error", [])
        codes = query.get("code", [])
        if len(errors) == 1 and not codes:
            raise CallbackError(
                errors[0] if errors[0] in {"access_denied", "invalid_scope"} else "provider_error"
            )
        if errors or len(codes) != 1 or not TOKEN_VALUE.fullmatch(codes[0]):
            raise CallbackError("invalid_callback")
        return codes[0]


async def discover(http: HttpClient) -> None:
    """Validate RFC 9728 and RFC 8414 metadata against the observed endpoints."""
    status, resource = await http.request("GET", f"{RESOURCE_URL}/mcp")
    if status == 404:
        status, resource = await http.request("GET", RESOURCE_URL)
    issuer = f"{OAUTH_ORIGIN}/"
    servers = resource.get("authorization_servers")
    scopes = resource.get("scopes_supported")
    if (
        status != 200
        or resource.get("resource") != MCP_URL
        or not isinstance(servers, list)
        or issuer not in servers
        or not isinstance(scopes, list)
        or ORDERS_SCOPE not in scopes
    ):
        raise ProtocolError("Invalid VkusVill protected resource metadata")
    status, metadata = await http.request("GET", METADATA_URL)
    methods = metadata.get("code_challenge_methods_supported")
    auth_methods = metadata.get("token_endpoint_auth_methods_supported")
    grants = metadata.get("grant_types_supported")
    if (
        status != 200
        or metadata.get("issuer") != issuer
        or metadata.get("authorization_endpoint") != AUTHORIZE_URL
        or metadata.get("token_endpoint") != TOKEN_URL
        or metadata.get("revocation_endpoint", f"{OAUTH_ORIGIN}/oauth2/revoke")
        != f"{OAUTH_ORIGIN}/oauth2/revoke"
        or not isinstance(methods, list)
        or "S256" not in methods
        or not isinstance(auth_methods, list)
        or "client_secret_post" not in auth_methods
        or not isinstance(grants, list)
        or not {"authorization_code", "refresh_token"}.issubset(
            method for method in grants if isinstance(method, str)
        )
    ):
        raise ProtocolError("Invalid VkusVill authorization server metadata")


async def _token_request(
    http: HttpClient, credentials: Credentials, params: dict[str, str], scope: str
) -> Token:
    status, value = await http.request(
        "POST",
        TOKEN_URL,
        form={
            **params,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
        },
    )
    error = value.get("error")
    if error == "invalid_scope" or status == 403:
        raise AccessDenied("VkusVill denied the requested OAuth scope")
    if status in (400, 401) or error in ("invalid_grant", "invalid_client", "unauthorized_client"):
        raise AuthError("VkusVill rejected the OAuth grant or client")
    if status != 200 or error:
        raise ProtocolError("Unexpected VkusVill OAuth response")
    return Token.from_response(value, scope)


async def exchange(
    http: HttpClient, credentials: Credentials, attempt: AuthorizationAttempt, code: str
) -> Token:
    """Exchange an authorization code using its original redirect and verifier."""
    return await _token_request(
        http,
        credentials,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": attempt.verifier,
            "redirect_uri": MANUAL_REDIRECT_URI,
        },
        REQUESTED_SCOPE,
    )


class TokenManager:
    """One refresh owner per config entry, with a durable replay guard."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, http: HttpClient) -> None:
        self.hass = hass
        self.entry = entry
        self.http = http
        self.credentials = Credentials(entry.data["client_id"], entry.data["client_secret"])
        self.lock = asyncio.Lock()
        # The journal contains a non-secret generation ID. HA can defer config entry
        # writes, so it must reach disk before an old refresh token can be consumed.
        self.journal = refresh_journal(hass, entry)
        self.checked = False
        self.blocked = False
        self.closed = False
        self.discovered = False

    async def get(self, rejected: Token | None = None) -> Token:
        """Return a usable token, refreshing once for the observed generation."""
        async with self.lock:
            if self.closed:
                raise AuthError("VkusVill session is closed")
            token = Token(**self.entry.data[CONF_TOKEN])
            if not self.checked:
                journal = await self.journal.async_load()
                if journal and journal.get("grant_id") == token.grant_id:
                    spent = journal.get("rotation")
                    self.blocked = type(spent) is not int or token.rotation <= spent
                self.checked = True
            if self.blocked:
                raise AuthError("VkusVill OAuth refresh requires a new sign-in")
            if token != rejected and time() < token.refresh_after:
                return token
            return await self._refresh(token)

    async def _refresh(self, token: Token) -> Token:
        if not self.discovered:
            await discover(self.http)
            self.discovered = True
        self.http.cooldown.check(OAUTH_ORIGIN)
        if self.hass.state is CoreState.stopping:
            raise ProtocolError("Home Assistant is stopping")
        await self._save_marker({"grant_id": token.grant_id, "rotation": token.rotation})
        self.blocked = True
        try:
            rotated = await _token_request(
                self.http,
                self.credentials,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                },
                token.scope,
            )
        except RateLimited as err:
            if err.status in (429, None):
                # A preflight or explicit rate-limit rejection did not issue a grant.
                await self._save_marker(
                    {"grant_id": token.grant_id, "rotation": token.rotation - 1}
                )
                self.blocked = False
            raise
        except ProtocolError, ConnectionError:
            raise AuthError("VkusVill OAuth refresh outcome is uncertain") from None
        if rotated.refresh_token == token.refresh_token:
            raise AuthError("VkusVill did not rotate the refresh token")
        current = Token(**self.entry.data[CONF_TOKEN])
        if current != token:
            # A successful reauth may have replaced the grant while this call was in flight.
            rotated = current
        else:
            rotated = replace(rotated, grant_id=token.grant_id, rotation=token.rotation + 1)
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_TOKEN: asdict(rotated)}
            )
        self.blocked = False
        return rotated

    async def _save_marker(self, marker: dict[str, str | int]) -> None:
        await self.journal.async_save(marker)
        if self.hass.state is CoreState.stopping or (
            await refresh_journal(self.hass, self.entry).async_load() != marker
        ):
            raise ProtocolError("Could not confirm VkusVill refresh journal write")

    async def close(self) -> None:
        """Prevent new refreshes and wait for an active rotation on unload."""
        self.closed = True
        async with self.lock:
            pass


def refresh_journal(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, str | int]]:
    """Return HA storage for the non-secret replay guard."""
    return Store(hass, 1, f"{DOMAIN}.refresh.{entry.entry_id}", atomic_writes=True)
