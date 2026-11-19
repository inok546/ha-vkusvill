"""Shared registry information for VkusVill entities."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME
from .coordinator import VkusVillConfigEntry, VkusVillCoordinator


class VkusVillEntity(CoordinatorEntity[VkusVillCoordinator]):
    """Entity associated with one VkusVill service entry."""

    _attr_has_entity_name = True

    def __init__(self, entry: VkusVillConfigEntry, platform: str, key: str) -> None:
        super().__init__(entry.runtime_data)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            entry_type=DeviceEntryType.SERVICE,
        )
        self.entity_id = f"{platform}.{DOMAIN}_{key}"
