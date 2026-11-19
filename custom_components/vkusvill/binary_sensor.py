"""Binary sensors backed by verified classifications."""

from collections.abc import Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VkusVillConfigEntry
from .entity import VkusVillEntity
from .models import OrderSnapshot


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VkusVillConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add tri-state binary sensors."""
    async_add_entities(
        [
            VkusVillBinarySensor(entry, "active_order", lambda data: data.has_active_order),
            VkusVillBinarySensor(entry, "courier", lambda data: data.with_courier),
        ]
    )


class VkusVillBinarySensor(VkusVillEntity, BinarySensorEntity):
    """Keep an unverified classification unknown."""

    def __init__(
        self, entry: VkusVillConfigEntry, key: str, value: Callable[[OrderSnapshot], bool | None]
    ) -> None:
        super().__init__(entry, "binary_sensor", key)
        self._value = value

    @property
    def is_on(self) -> bool | None:
        """Return a verified boolean or unknown."""
        return self._value(self.coordinator.data)
