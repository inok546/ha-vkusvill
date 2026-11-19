"""Privacy-preserving diagnostics for VkusVill."""

from datetime import UTC, datetime

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .const import VERSION
from .coordinator import VkusVillConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VkusVillConfigEntry
) -> dict[str, object]:
    """Build an allowlist of non-personal diagnostic fields."""
    result: dict[str, object] = {
        "version": VERSION,
        "loaded": entry.state is ConfigEntryState.LOADED,
    }
    if entry.state is ConfigEntryState.LOADED:
        coordinator = entry.runtime_data
        snapshot = coordinator.data
        result.update(
            connected=coordinator.last_update_success,
            last_error_type=coordinator.last_error_type,
            seconds_since_success=max(
                0, int((datetime.now(UTC) - snapshot.fetched_at).total_seconds())
            ),
            orders_count=len(snapshot.history.orders),
            pagination_complete=snapshot.history.complete,
        )
    return result
