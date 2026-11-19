"""Binary sensors backed by verified classifications."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import VkusVillConfigEntry
from .entity import VkusVillEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VkusVillConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the active-order sensor and drop the duplicate courier entity."""
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_courier"
    ):
        registry.async_remove(entity_id)
    async_add_entities([VkusVillActiveOrderSensor(entry)])


class VkusVillActiveOrderSensor(VkusVillEntity, BinarySensorEntity):
    """Keep an unverified classification unknown."""

    def __init__(self, entry: VkusVillConfigEntry) -> None:
        super().__init__(entry, "binary_sensor", "active_order")

    @property
    def is_on(self) -> bool | None:
        """Return a verified boolean or unknown."""
        return self.coordinator.data.has_active_order
