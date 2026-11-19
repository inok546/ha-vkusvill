"""Track the text to show during an order and after its observed completion."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hmac import digest

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.dt import parse_datetime, utcnow

from .const import CONF_TOKEN, DOMAIN
from .models import ACTIVE, TERMINAL, OrderSnapshot, newest_order

CARD_HOLD = timedelta(minutes=60)


@dataclass(frozen=True)
class CardStatus:
    """Raw text for the card and its cutoff if the order is finished."""

    text: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class _Completion:
    fingerprint: str
    expires_at: datetime
    text: str | None = None


def card_store(hass: HomeAssistant, entry: ConfigEntry) -> Store[dict[str, str]]:
    """Store only a deadline and a grant-scoped order fingerprint."""
    return Store(hass, 1, f"{DOMAIN}.order_card.{entry.entry_id}", private=True, atomic_writes=True)


class OrderCardTracker:
    """Track observed transitions independently of the history fallback."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._store = card_store(hass, entry)
        self._key = entry.data[CONF_TOKEN]["grant_id"].encode()
        self._active: set[str] = set()
        self._completion: _Completion | None = None

    def _fingerprint(self, key: str) -> str:
        return digest(self._key, key.encode(), "sha256").hex()

    async def async_load(self) -> None:
        """Restore a running grace period, with status text restored from live data."""
        saved = await self._store.async_load()
        if not saved:
            return
        try:
            expires_at = parse_datetime(saved["expires_at"])
            fingerprint = saved["fingerprint"]
        except KeyError, ValueError, TypeError:
            return
        now = utcnow()
        if expires_at and expires_at.tzinfo and now < expires_at <= now + CARD_HOLD:
            self._completion = _Completion(fingerprint, expires_at)

    async def async_observe(self, snapshot: OrderSnapshot) -> CardStatus | None:
        """Start a grace period for a confirmed active-to-terminal transition."""
        orders = snapshot.history.orders
        completed = newest_order(
            order for order in orders if order.key in self._active and order.stage in TERMINAL
        )
        self._active = {
            order.key
            for order in orders
            if order.key is not None
            and (
                order.stage in ACTIVE or (order.key in self._active and order.stage not in TERMINAL)
            )
        }
        if completed is not None and completed.key is not None:
            completion = _Completion(
                self._fingerprint(completed.key),
                snapshot.fetched_at + CARD_HOLD,
                completed.status_name,
            )
            self._completion = completion
            await self._store.async_save(
                {
                    "fingerprint": completion.fingerprint,
                    "expires_at": completion.expires_at.isoformat(),
                }
            )
        elif (completion := self._completion) is not None:
            if completion.expires_at <= snapshot.fetched_at or any(
                self._fingerprint(key) == completion.fingerprint for key in self._active
            ):
                self._completion = None
                await self._store.async_save({})
            else:
                observed = next(
                    (
                        order
                        for order in orders
                        if order.key is not None
                        and order.stage in TERMINAL
                        and self._fingerprint(order.key) == completion.fingerprint
                    ),
                    None,
                )
                if observed is not None and observed.status_name != completion.text:
                    self._completion = replace(completion, text=observed.status_name)

        if snapshot.active_order and snapshot.active_order.status_name:
            return CardStatus(snapshot.active_order.status_name)
        if (completion := self._completion) and completion.text:
            return CardStatus(completion.text, completion.expires_at)
        return None
