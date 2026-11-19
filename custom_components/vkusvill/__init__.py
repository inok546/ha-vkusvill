"""Monitor VkusVill orders through its official MCP service."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.hass_dict import HassKey

from .api import VkusVillClient
from .const import DOMAIN
from .coordinator import VkusVillConfigEntry, VkusVillCoordinator
from .http import Cooldown, HttpClient
from .oauth import TokenManager, refresh_journal
from .order_card import card_store

PLATFORMS = (Platform.SENSOR, Platform.BINARY_SENSOR)
COOLDOWN: HassKey[Cooldown] = HassKey(f"{DOMAIN}.cooldown")


def http_client(hass: HomeAssistant) -> HttpClient:
    """Use the HA-managed session and share cooldowns over reload and setup."""
    return HttpClient(async_get_clientsession(hass), hass.data.setdefault(COOLDOWN, Cooldown()))


async def async_setup_entry(hass: HomeAssistant, entry: VkusVillConfigEntry) -> bool:
    """Set up one polling coordinator for the entry."""
    http = http_client(hass)
    coordinator = VkusVillCoordinator(
        hass, entry, VkusVillClient(http, TokenManager(hass, entry, http))
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: VkusVillConfigEntry) -> bool:
    """Unload entities before disposing of the entry's coordinator."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: VkusVillConfigEntry) -> None:
    """Remove the entry's integration storage."""
    await refresh_journal(hass, entry).async_remove()
    await card_store(hass, entry).async_remove()
