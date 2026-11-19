"""UI onboarding with a state-checked manual PKCE callback."""

import re
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import http_client
from .api import fetch_order_page
from .const import (
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUAL_REDIRECT_URI,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    NAME,
    REGISTRATION_URL,
)
from .http import AccessDenied, AuthError, RateLimited, VkusVillError
from .oauth import AuthorizationAttempt, CallbackError, Credentials, discover, exchange

SECRET_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class VkusVillConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure VkusVill or renew an existing grant."""

    def __init__(self) -> None:
        self._attempt: AuthorizationAttempt | None = None
        self._credentials: Credentials | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the registered personal client credentials."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._client_step("user", user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start the same PKCE flow for the existing application."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the existing client secret to be replaced during reauth."""
        return await self._client_step("reauth_confirm", user_input)

    async def _client_step(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth = step == "reauth_confirm"
        schema = (
            {vol.Optional("client_secret"): SECRET_FIELD}
            if reauth
            else {
                vol.Required("client_id"): TextSelector(),
                vol.Required("client_secret"): SECRET_FIELD,
            }
        )
        if user_input is not None:
            if reauth:
                data = self._get_reauth_entry().data
                client_id = data["client_id"]
                secret = user_input.get("client_secret", "").strip() or data["client_secret"]
            else:
                client_id = user_input["client_id"].strip()
                secret = user_input["client_secret"].strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{4,200}", client_id) or not re.fullmatch(
                r"[!-~]{1,2048}", secret
            ):
                errors["base"] = "invalid_credentials"
            else:
                try:
                    await discover(http_client(self.hass))
                except RateLimited:
                    errors["base"] = "rate_limited"
                except VkusVillError:
                    errors["base"] = "discovery_failed"
                else:
                    self._credentials = Credentials(client_id, secret)
                    self._attempt = AuthorizationAttempt.create()
                    return await self.async_step_callback()
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "registration_url": REGISTRATION_URL,
                "redirect_uri": MANUAL_REDIRECT_URI,
            },
        )

    async def async_step_callback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept the complete callback and validate the grant against MCP."""
        attempt = self._attempt
        credentials = self._credentials
        if attempt is None or credentials is None:
            return self.async_abort(reason="expired")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                code = attempt.code_from_callback(user_input["callback_url"])
            except CallbackError as err:
                if str(err) != "invalid_callback":
                    self._clear_attempt()
                    return self.async_abort(reason=str(err))
                errors["callback_url"] = "invalid_callback"
            else:
                # Never send a successfully parsed authorization code twice.
                self._clear_attempt()
                http = http_client(self.hass)
                try:
                    token = await exchange(http, credentials, attempt, code)
                    await fetch_order_page(http, token, 1, credentials.client_secret)
                except AccessDenied:
                    return self.async_abort(reason="invalid_scope")
                except AuthError:
                    return self.async_abort(reason="authorization_failed")
                except RateLimited:
                    return self.async_abort(reason="rate_limited")
                except VkusVillError:
                    return self.async_abort(reason="connection_failed")
                data = {
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    CONF_TOKEN: asdict(token),
                }
                if self.source == "reauth":
                    return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=data)
        return self.async_show_form(
            step_id="callback",
            data_schema=vol.Schema({vol.Required("callback_url"): SECRET_FIELD}),
            errors=errors,
            description_placeholders={
                "authorize_url": attempt.url(credentials),
                "redirect_uri": MANUAL_REDIRECT_URI,
            },
        )

    def _clear_attempt(self) -> None:
        self._attempt = None
        self._credentials = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> VkusVillOptionsFlow:
        """Build the interval options flow."""
        return VkusVillOptionsFlow()


class VkusVillOptionsFlow(OptionsFlowWithReload):
    """Configure the poll interval via the integration's options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Save the validated interval."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(int, vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)),
                }
            ),
        )
