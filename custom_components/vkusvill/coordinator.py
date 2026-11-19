"""Poll VkusVill through a single coordinator."""

import logging
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VkusVillClient
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .http import AccessDenied, AuthError, RateLimited, VkusVillError
from .models import OrderSnapshot, select_orders

_LOGGER = logging.getLogger(__name__)

type VkusVillConfigEntry = ConfigEntry[VkusVillCoordinator]


class VkusVillCoordinator(DataUpdateCoordinator[OrderSnapshot]):
    """Fetch the order history and derive a conservative snapshot."""

    def __init__(
        self, hass: HomeAssistant, entry: VkusVillConfigEntry, client: VkusVillClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.client = client
        self.last_error_type: str | None = None
        self.auth_failed = False

    async def _async_update_data(self) -> OrderSnapshot:
        if self.auth_failed:
            raise ConfigEntryAuthFailed("VkusVill authorization must be renewed")
        try:
            history = await self.client.get_history()
        except AccessDenied as err:
            self.last_error_type = type(err).__name__
            self.auth_failed = True
            raise ConfigEntryAuthFailed("VkusVill denied access to order history") from None
        except AuthError as err:
            self.last_error_type = type(err).__name__
            self.auth_failed = True
            raise ConfigEntryAuthFailed("VkusVill authorization must be renewed") from None
        except RateLimited as err:
            self.last_error_type = type(err).__name__
            raise UpdateFailed(str(err), retry_after=min(err.seconds, 3600)) from None
        except VkusVillError as err:
            self.last_error_type = type(err).__name__
            raise UpdateFailed(str(err)) from None
        except TimeoutError:
            self.last_error_type = "TimeoutError"
            raise UpdateFailed("VkusVill order history update timed out") from None
        self.last_error_type = None
        return select_orders(history, datetime.now(UTC))

    async def async_shutdown(self) -> None:
        """Stop the scheduler and wait for pending traffic before reloading."""
        await super().async_shutdown()
        await self.client.close()
