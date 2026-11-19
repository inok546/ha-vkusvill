"""Poll VkusVill through a single coordinator."""

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import VkusVillClient
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .http import AccessDenied, AuthError, RateLimited, VkusVillError
from .models import OrderSnapshot, Stage, select_orders
from .order_card import CardStatus, OrderCardTracker

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
        self.card_status: CardStatus | None = None
        self._card_tracker = OrderCardTracker(hass, entry)
        self._cancel_card_expiry: CALLBACK_TYPE | None = None
        self._closed = False

    async def _async_setup(self) -> None:
        await self._card_tracker.async_load()

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
            _LOGGER.debug(
                "Polling paused: http_status=%s, retry_after_seconds=%.0f", err.status, err.seconds
            )
            raise UpdateFailed(str(err), retry_after=min(err.seconds, 3600)) from None
        except VkusVillError as err:
            self.last_error_type = type(err).__name__
            raise UpdateFailed(str(err)) from None
        except TimeoutError:
            self.last_error_type = "TimeoutError"
            raise UpdateFailed("VkusVill order history update timed out") from None
        self.last_error_type = None
        snapshot = select_orders(history, datetime.now(UTC))
        card = await self._card_tracker.async_observe(snapshot)
        self._set_card_status(card)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            previous = self.data.selected if self.data is not None else None
            present = snapshot.selected
            counts = Counter(order.status.active for order in history.orders)
            same_order = None
            if previous and previous.key and present and present.key:
                same_order = previous.key == present.key
            _LOGGER.debug(
                "Orders polled: count=%d, complete=%s, selected_stage=%s, "
                "raw_status_present=%s, raw_status_changed=%s, card=%s, "
                "same_order=%s, active=%d, terminal=%d, unclassified=%d, without_created_at=%d, "
                "delivery_time_fields=%s, lifecycle_fields=%s",
                len(history.orders),
                history.complete,
                present.stage if present else Stage.UNKNOWN,
                bool(present and present.status_name),
                (present.status_name if present else None)
                != (previous.status_name if previous else None),
                ("grace_period" if card.expires_at else "active") if card else "hidden",
                same_order,
                counts[True],
                counts[False],
                counts[None],
                sum(order.created_at is None for order in history.orders),
                present.delivery_time_fields if present else (),
                present.lifecycle_fields if present else (),
            )
        return snapshot

    @callback
    def _set_card_status(self, card: CardStatus | None) -> None:
        if self._closed or card == self.card_status:
            return
        if self._cancel_card_expiry is not None:
            self._cancel_card_expiry()
            self._cancel_card_expiry = None
        self.card_status = card
        if card is not None and card.expires_at is not None:
            self._cancel_card_expiry = async_track_point_in_utc_time(
                self.hass, self._expire_card, card.expires_at
            )

    @callback
    def _expire_card(self, _now: datetime) -> None:
        self._cancel_card_expiry = None
        self.card_status = None
        _LOGGER.debug("Order card grace period expired")
        self.async_update_listeners()

    async def async_shutdown(self) -> None:
        """Stop the scheduler and wait for pending traffic before reloading."""
        self._closed = True
        if self._cancel_card_expiry is not None:
            self._cancel_card_expiry()
            self._cancel_card_expiry = None
        await super().async_shutdown()
        await self.client.close()
