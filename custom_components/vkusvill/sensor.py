"""VkusVill order and polling sensors."""

from collections.abc import Callable
from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import VkusVillConfigEntry
from .entity import VkusVillEntity
from .models import OrderSnapshot, Stage
from .order_card import CardStatus


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VkusVillConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a fixed set of sensors for the service entry."""
    async_add_entities(
        [
            VkusVillSensor(
                entry,
                SensorEntityDescription(key="order_status", icon="mdi:receipt-text"),
                lambda data: data.selected.status_name if data.selected else None,
            ),
            VkusVillOrderCardSensor(entry),
            VkusVillSensor(
                entry,
                SensorEntityDescription(
                    key="order_stage",
                    device_class=SensorDeviceClass.ENUM,
                    options=[stage.value for stage in Stage],
                ),
                lambda data: data.selected.stage.value if data.selected else None,
            ),
            VkusVillSensor(
                entry,
                SensorEntityDescription(key="order_id", icon="mdi:pound"),
                lambda data: data.selected.number if data.selected else None,
            ),
            VkusVillSensor(
                entry,
                SensorEntityDescription(key="active_order_id", icon="mdi:pound"),
                lambda data: data.active_order.number if data.active_order else None,
            ),
            VkusVillSensor(
                entry,
                SensorEntityDescription(
                    key="last_order_created_at", device_class=SensorDeviceClass.TIMESTAMP
                ),
                lambda data: data.last_order_created_at,
            ),
            VkusVillSensor(
                entry,
                SensorEntityDescription(
                    key="last_successful_update",
                    device_class=SensorDeviceClass.TIMESTAMP,
                    entity_category=EntityCategory.DIAGNOSTIC,
                ),
                lambda data: data.fetched_at,
            ),
        ]
    )


class VkusVillOrderCardSensor(VkusVillEntity, SensorEntity):
    """Expose the raw status while the order card should be visible."""

    _attr_icon = "mdi:receipt-text"

    def __init__(self, entry: VkusVillConfigEntry) -> None:
        super().__init__(entry, "sensor", "order_card_status")

    @property
    def native_value(self) -> str | None:
        """Return the card text, if an order is in its display window."""
        card: CardStatus | None = self.coordinator.card_status
        return card.text if card else None


class VkusVillSensor(VkusVillEntity, SensorEntity):
    """Sensor backed by the most recent successful snapshot."""

    def __init__(
        self,
        entry: VkusVillConfigEntry,
        description: SensorEntityDescription,
        value: Callable[[OrderSnapshot], str | datetime | None],
    ) -> None:
        super().__init__(entry, "sensor", description.key)
        self.entity_description = description
        self._value = value

    @property
    def native_value(self) -> str | datetime | None:
        """Return the value, if present."""
        return self._value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose selected order time and poll freshness for the raw status."""
        if self.entity_description.key != "order_status":
            return None
        snapshot = self.coordinator.data
        attributes = {"last_successful_update": snapshot.fetched_at.isoformat()}
        if snapshot.selected:
            attributes["order_stage"] = snapshot.selected.stage.value
            if snapshot.selected.created_at:
                attributes["order_created_at"] = snapshot.selected.created_at.isoformat()
        return attributes
