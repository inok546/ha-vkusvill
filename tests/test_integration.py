"""Use real Home Assistant config flows, coordinator, and entity registry."""

import json
from base64 import urlsafe_b64encode
from hashlib import sha256
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.vkusvill.const import (
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DOMAIN,
    MANUAL_REDIRECT_URI,
    MCP_URL,
)
from custom_components.vkusvill.diagnostics import async_get_config_entry_diagnostics
from custom_components.vkusvill.oauth import AUTHORIZE_URL, TOKEN_URL, refresh_journal

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
STATUS = "sensor.vkusvill_order_status"
STAGE = "sensor.vkusvill_order_stage"
ORDER = "sensor.vkusvill_order_id"
ACTIVE_ID = "sensor.vkusvill_active_order_id"
ACTIVE = "binary_sensor.vkusvill_active_order"
COURIER = "binary_sensor.vkusvill_courier"
LAST_UPDATE = "sensor.vkusvill_last_successful_update"


def consent(flow, code=None, error=None):
    authorize_url = flow["description_placeholders"]["authorize_url"]
    assert authorize_url.startswith(AUTHORIZE_URL)
    state = parse_qs(urlsplit(authorize_url).query)["state"][0]
    query = {"state": state, "code": code} if code else {"state": state, "error": error}
    return {"callback_url": f"{MANUAL_REDIRECT_URI}?{urlencode(query)}"}


async def begin_flow(hass, wire, entry_data):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    wire.discovery()
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"client_id": entry_data["client_id"], "client_secret": entry_data["client_secret"]},
    )


async def test_config_flow_setup_and_options(hass, wire, oauth_response, entry_data):
    flow = await begin_flow(hass, wire, entry_data)
    assert flow["step_id"] == "callback"
    code = token_urlsafe(24)
    bad = await hass.config_entries.flow.async_configure(flow["flow_id"], {"callback_url": code})
    assert bad["errors"] == {"callback_url": "invalid_callback"}
    wire.json("POST", TOKEN_URL, oauth_response)
    wire.orders([])
    wire.orders([{"id": 21, "status_name": "Статус MCP"}])
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], consent(flow, code))
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    oauth_form = next(kwargs["data"] for _, url, kwargs in wire.calls if url == TOKEN_URL)
    query = parse_qs(urlsplit(flow["description_placeholders"]["authorize_url"]).query)
    assert set(query["scope"][0].split()) == {"purchase_history.read", "offline_access"}
    assert query["code_challenge_method"] == ["S256"]
    challenge = (
        urlsafe_b64encode(sha256(oauth_form["code_verifier"].encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [challenge]
    assert oauth_form["code"] == code
    assert oauth_form["redirect_uri"] == MANUAL_REDIRECT_URI
    assert hass.states.get(STATUS).state == "Статус MCP"
    assert hass.states.get(ORDER).state == "21"
    assert hass.states.get(STAGE).state == STATE_UNKNOWN
    assert hass.states.get(ACTIVE_ID).state == STATE_UNKNOWN
    assert hass.states.get(ACTIVE).state == STATE_UNKNOWN
    assert hass.states.get(COURIER).state == STATE_UNKNOWN
    assert hass.states.get(LAST_UPDATE).state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}
    assert entry.runtime_data.update_interval.total_seconds() == 120

    options = await hass.config_entries.options.async_init(entry.entry_id)
    wire.orders([])
    result = await hass.config_entries.options.async_configure(
        options["flow_id"], {CONF_SCAN_INTERVAL: 240}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.runtime_data.update_interval.total_seconds() == 240
    assert hass.states.get(STATUS).state == STATE_UNKNOWN
    assert hass.states.get(ACTIVE).state == STATE_UNKNOWN


async def test_denial_and_missing_refresh_token(hass, wire, oauth_response, entry_data):
    flow = await begin_flow(hass, wire, entry_data)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], consent(flow, error="access_denied")
    )
    assert result["reason"] == "access_denied"
    assert not hass.config_entries.async_entries(DOMAIN)
    flow = await begin_flow(hass, wire, entry_data)
    response = {key: val for key, val in oauth_response.items() if key != "refresh_token"}
    wire.json("POST", TOKEN_URL, response)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], consent(flow, code=token_urlsafe(24))
    )
    assert result["reason"] == "authorization_failed"
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_refresh_availability_redaction_and_unload_reload(
    hass, entry, entry_data, wire, caplog
):
    private_order = "5982429221"
    private_status = "Статус только для дисплея"
    grant = entry_data[CONF_TOKEN]
    wire.orders(
        [
            {
                "id_general": private_order,
                "status_name": private_status,
                "products": ["Private products"],
                "address": "Private address",
            }
        ]
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get(STATUS).unique_id == f"{entry.entry_id}_order_status"
    assert hass.states.get(STATUS).state == private_status
    assert private_order not in json.dumps(dict(hass.states.get(STATUS).attributes))

    wire.json(
        "POST",
        MCP_URL,
        {
            "id": 1,
            "error": {
                "message": " ".join(
                    (grant["access_token"], grant["refresh_token"], entry_data["client_secret"])
                ),
                "code": -32000,
            },
        },
    )
    await entry.runtime_data.async_refresh()
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE
    assert hass.states.get(ACTIVE).state == STATE_UNAVAILABLE
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["last_error_type"] == "RpcError"
    assert diag["orders_count"] == 1
    for sensitive in (
        grant["access_token"],
        grant["refresh_token"],
        entry_data["client_secret"],
        private_order,
        private_status,
        "Private products",
        "Private address",
    ):
        assert sensitive not in str(diag)
        assert sensitive not in caplog.text

    first = entry.runtime_data
    wire.orders([{"id_general": private_order, "status_name": private_status}])
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not first
    assert hass.states.get(STATUS).state == private_status
    assert registry.async_get(STATUS).unique_id == f"{entry.entry_id}_order_status"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE
    assert hass.states.get(STATUS).attributes["restored"] is True
    wire.orders([])
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert hass.states.get(STATUS).state == STATE_UNKNOWN


async def test_reauth_on_forbidden_keeps_entry_and_reloads(hass, entry, wire, oauth_response):
    wire.orders([])
    assert await hass.config_entries.async_setup(entry.entry_id)
    wire.status(MCP_URL, 403)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flow["context"]["source"] == SOURCE_REAUTH
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert wire.pages == [1, 1]
    assert len(hass.config_entries.flow.async_progress_by_handler(DOMAIN)) == 1
    wire.discovery()
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    wire.json("POST", TOKEN_URL, oauth_response)
    wire.orders([])
    wire.orders([{"id": 27, "status_name": "Новый статус"}])
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], consent(flow, code=token_urlsafe(24))
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reauth_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert hass.states.get(STATUS).state == "Новый статус"
    assert entry.data[CONF_TOKEN]["access_token"] == oauth_response["access_token"]


async def test_remove_entry_cleans_refresh_journal(hass, entry, wire):
    wire.orders([])
    assert await hass.config_entries.async_setup(entry.entry_id)
    journal = refresh_journal(hass, entry)
    await journal.async_save({"grant_id": entry.data[CONF_TOKEN]["grant_id"], "rotation": 0})
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await journal.async_load() is None
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_rate_limit_survives_reload_without_external_requests(hass, entry, wire, monkeypatch):
    clock = 100.0
    monkeypatch.setattr("custom_components.vkusvill.http.monotonic", lambda: clock)
    wire.orders([])
    assert await hass.config_entries.async_setup(entry.entry_id)
    wire.status(MCP_URL, 503, **{"Retry-After": "75"})
    await entry.runtime_data.async_refresh()
    assert hass.states.get(STATUS).state == STATE_UNAVAILABLE
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert wire.pages == [1, 1]
    clock += 76
    wire.orders([{"id": 27, "status_name": "Returned after cooldown"}])
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert hass.states.get(STATUS).state == "Returned after cooldown"
