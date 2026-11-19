"""OAuth discovery, callback validation, and rotation across concurrent use."""

import asyncio
import json
from dataclasses import asdict
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from custom_components.vkusvill import http_client
from custom_components.vkusvill.api import VkusVillClient
from custom_components.vkusvill.const import CONF_TOKEN, MANUAL_REDIRECT_URI, MCP_URL, OAUTH_ORIGIN
from custom_components.vkusvill.http import (
    AccessDenied,
    AuthError,
    ProtocolError,
    RateLimited,
    Unauthorized,
)
from custom_components.vkusvill.oauth import (
    RESOURCE_URL,
    TOKEN_URL,
    AuthorizationAttempt,
    CallbackError,
    Credentials,
    Token,
    TokenManager,
    discover,
    refresh_journal,
)

from .conftest import Response


def test_callback_checks_exact_url_state_and_expiry(monkeypatch):
    attempt = AuthorizationAttempt.create()
    code = token_urlsafe(16)
    correct = f"{MANUAL_REDIRECT_URI}?{urlencode({'state': attempt.state, 'code': code})}"
    assert attempt.code_from_callback(correct) == code
    for invalid in (
        code,
        correct.replace("localhost", "evil.invalid"),
        f"{correct}&code={code}",
        f"{correct}&state=other",
        correct.replace(attempt.state, "different"),
        f"{correct}#fragment",
        correct.replace(attempt.state, "%E2%98%83"),
    ):
        with pytest.raises(CallbackError, match="invalid_callback"):
            attempt.code_from_callback(invalid)
    denied = f"{MANUAL_REDIRECT_URI}?state={attempt.state}&error=access_denied"
    with pytest.raises(CallbackError, match="access_denied"):
        attempt.code_from_callback(denied)
    assert (
        "offline_access"
        in parse_qs(urlsplit(attempt.url(Credentials("test-client", token_urlsafe(16)))).query)[
            "scope"
        ][0].split()
    )
    monkeypatch.setattr("custom_components.vkusvill.oauth.monotonic", lambda: attempt.deadline)
    with pytest.raises(CallbackError, match="expired"):
        attempt.code_from_callback(correct)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://oauth.vkusvill.ru/oauth2/token",
        "https://mcp.vkusvill.ru/oauth2/token",
        "https://oauth.vkusvill.ru.evil.invalid/oauth2/token",
        "https://oauth.vkusvill.ru:443/oauth2/token",
        "https://oauth.vkusvill.ru/oauth2/token?x=1",
    ],
)
async def test_metadata_rejects_unexpected_endpoint(hass, wire, endpoint):
    wire.json(
        "GET",
        f"{RESOURCE_URL}/mcp",
        {
            "resource": MCP_URL,
            "authorization_servers": [f"{OAUTH_ORIGIN}/"],
            "scopes_supported": ["purchase_history.read"],
        },
    )
    wire.metadata(token_endpoint=endpoint)
    with pytest.raises(ProtocolError, match="metadata"):
        await discover(http_client(hass))
    assert [method for method, _, _ in wire.calls] == ["GET", "GET"]


async def test_discovery_falls_back_to_origin_metadata(hass, wire):
    wire.json("GET", f"{RESOURCE_URL}/mcp", {}, status=404)
    wire.json(
        "GET",
        RESOURCE_URL,
        {
            "resource": MCP_URL,
            "authorization_servers": [f"{OAUTH_ORIGIN}/"],
            "scopes_supported": ["purchase_history.read"],
        },
    )
    wire.metadata()
    await discover(http_client(hass))
    assert len(wire.calls) == 3


async def test_concurrent_refresh_commits_pair_and_guards_stale_restart(
    hass, entry, entry_data, wire, oauth_response
):
    manager = TokenManager(hass, entry, http_client(hass))
    before = entry_data[CONF_TOKEN]
    wire.discovery()
    inflight = asyncio.Event()
    finish = asyncio.Event()

    async def rotate(kwargs):
        assert kwargs["data"]["refresh_token"] == before["refresh_token"]
        assert kwargs["data"]["client_secret"] == entry_data["client_secret"]
        journal = await refresh_journal(hass, entry).async_load()
        assert journal == {"grant_id": before["grant_id"], "rotation": 0}
        inflight.set()
        await finish.wait()
        return Response(raw=json.dumps(oauth_response).encode())

    wire.add("POST", TOKEN_URL, rotate)
    first = asyncio.create_task(manager.get(rejected=Token(**before)))
    await inflight.wait()
    second = asyncio.create_task(manager.get(rejected=Token(**before)))
    finish.set()
    tokens = await asyncio.gather(first, second)
    assert tokens[0] == tokens[1]
    assert tokens[0].refresh_token == oauth_response["refresh_token"]
    assert tokens[0].access_token == oauth_response["access_token"]
    assert entry.data[CONF_TOKEN] == asdict(tokens[0])
    assert [call[1] for call in wire.calls].count(TOKEN_URL) == 1
    second_response = {**oauth_response, "refresh_token": token_urlsafe(24)}
    wire.json("POST", TOKEN_URL, second_response)
    latest = await manager.get(rejected=tokens[0])
    assert latest.rotation == 2
    await manager.close()
    # New manager can load the committed generation after a restart.
    assert await TokenManager(hass, entry, http_client(hass)).get() == latest
    # A stale config entry after interruption must not submit the old token.
    hass.config_entries.async_update_entry(entry, data=entry_data)
    with pytest.raises(AuthError, match="new sign-in"):
        await TokenManager(hass, entry, http_client(hass)).get()
    assert len(wire.calls) == 4


async def test_refresh_timeout_is_not_replayed(hass, entry, wire):
    http = http_client(hass)
    manager = TokenManager(hass, entry, http)
    wire.discovery()
    wire.add("POST", TOKEN_URL, TimeoutError())
    with pytest.raises(AuthError, match="outcome is uncertain"):
        await manager.get(rejected=Token(**entry.data[CONF_TOKEN]))
    with pytest.raises(AuthError, match="new sign-in"):
        await TokenManager(hass, entry, http).get()
    assert [call[1] for call in wire.calls].count(TOKEN_URL) == 1


@pytest.mark.parametrize("status", [429, 503])
async def test_token_endpoint_rate_limit(hass, entry, wire, oauth_response, monkeypatch, status):
    clock = 100.0
    monkeypatch.setattr("custom_components.vkusvill.http.monotonic", lambda: clock)
    manager = TokenManager(hass, entry, http_client(hass))
    rejected = Token(**entry.data[CONF_TOKEN])
    wire.discovery()
    wire.status(TOKEN_URL, status, **{"Retry-After": "75"})
    with pytest.raises(RateLimited):
        await manager.get(rejected=rejected)
    if status == 429:
        with pytest.raises(RateLimited):
            await manager.get(rejected=rejected)
        clock += 76
        wire.json("POST", TOKEN_URL, oauth_response)
        assert (await manager.get(rejected=rejected)).access_token == oauth_response["access_token"]
    else:
        with pytest.raises(AuthError, match="new sign-in"):
            await manager.get(rejected=rejected)


@pytest.mark.parametrize(
    ("status", "header"),
    [(403, {}), (401, {"WWW-Authenticate": "Bearer error=insufficient_scope"})],
)
async def test_missing_scope_never_refreshes(hass, entry, wire, status, header):
    http = http_client(hass)
    wire.status(MCP_URL, status, **header)
    with pytest.raises(AccessDenied):
        await VkusVillClient(http, TokenManager(hass, entry, http)).get_history()
    assert len(wire.calls) == 1


async def test_second_unauthorized_fails_without_rotating_again(hass, entry, wire, oauth_response):
    http = http_client(hass)
    wire.status(MCP_URL, 401)
    wire.discovery()
    wire.json("POST", TOKEN_URL, oauth_response)
    wire.status(MCP_URL, 401)
    with pytest.raises(Unauthorized):
        await VkusVillClient(http, TokenManager(hass, entry, http)).get_history()
    assert wire.pages == [1, 1]
    assert entry.data[CONF_TOKEN]["refresh_token"] == oauth_response["refresh_token"]
    assert (
        wire.calls[-1][2]["headers"]["Authorization"] == f"Bearer {oauth_response['access_token']}"
    )


async def test_failed_journal_write_stops_refresh(hass, entry, wire, monkeypatch):
    manager = TokenManager(hass, entry, http_client(hass))

    async def drop_write(*args):
        pass

    monkeypatch.setattr(manager.journal, "async_save", drop_write)
    wire.discovery()
    with pytest.raises(ProtocolError, match="journal"):
        await manager.get(rejected=Token(**entry.data[CONF_TOKEN]))
    assert not any(url == TOKEN_URL for _, url, _ in wire.calls)


async def test_reauth_replacement_wins_over_inflight_refresh(hass, entry, wire, oauth_response):
    manager = TokenManager(hass, entry, http_client(hass))
    before = Token(**entry.data[CONF_TOKEN])
    wire.discovery()
    replacement = Token.from_response(
        {
            **oauth_response,
            "access_token": token_urlsafe(24),
            "refresh_token": token_urlsafe(24),
        },
        before.scope,
    )

    async def rotate(_kwargs):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TOKEN: asdict(replacement)}
        )
        return Response(raw=json.dumps(oauth_response).encode())

    wire.add("POST", TOKEN_URL, rotate)
    assert await manager.get(rejected=before) == replacement
    assert entry.data[CONF_TOKEN] == asdict(replacement)
    assert await TokenManager(hass, entry, http_client(hass)).get() == replacement
