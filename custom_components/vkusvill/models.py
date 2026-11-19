"""Order models and evidence-based status selection."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Stage(StrEnum):
    """Supported normalized order stages."""

    ASSEMBLING = "assembling"
    WITH_COURIER = "with_courier"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    OTHER = "other"
    UNKNOWN = "unknown"


# Add exact values only with a source in docs/API_FINDINGS.md.
VERIFIED_STATUSES: Mapping[str, Stage] = {}
ACTIVE = {Stage.ASSEMBLING, Stage.WITH_COURIER}
TERMINAL = {Stage.DELIVERED, Stage.CANCELLED}


@dataclass(frozen=True)
class Order:
    """Minimal fields required for selection and entity state."""

    key: str | None
    number: str | None
    status_name: str | None
    created_at: datetime | None

    @property
    def stage(self) -> Stage:
        """Classify an exact, verified status."""
        return (
            VERIFIED_STATUSES.get(self.status_name, Stage.UNKNOWN)
            if self.status_name
            else Stage.UNKNOWN
        )


@dataclass(frozen=True)
class OrderPage:
    """One parsed page and its pagination hint."""

    items: tuple[Order, ...]
    has_more: bool | None


@dataclass(frozen=True)
class OrderHistory:
    """Orders from a successful, bounded history traversal."""

    orders: tuple[Order, ...]
    complete: bool


@dataclass(frozen=True)
class OrderSnapshot:
    """A successful poll and its deterministically selected order."""

    history: OrderHistory
    selected: Order | None
    active_order: Order | None
    has_active_order: bool | None
    last_order_created_at: datetime | None
    fetched_at: datetime

    @property
    def with_courier(self) -> bool | None:
        """Report courier status for the selected verified active order."""
        if self.active_order:
            return self.active_order.stage == Stage.WITH_COURIER
        return False if self.has_active_order is False else None


def _sort_key(order: Order) -> tuple[float, str, str]:
    return (
        order.created_at.timestamp() if order.created_at else float("-inf"),
        order.key or order.number or "",
        order.status_name or "",
    )


def select_orders(history: OrderHistory, fetched_at: datetime) -> OrderSnapshot:
    """Prefer the newest verified active order, falling back to history."""
    orders = history.orders
    active = max((order for order in orders if order.stage in ACTIVE), key=_sort_key, default=None)
    has_active = None
    if active is not None:
        has_active = True
    elif history.complete and orders and all(order.stage in TERMINAL for order in orders):
        has_active = False
    latest = max(orders, key=_sort_key, default=None)
    return OrderSnapshot(
        history=history,
        selected=active or latest,
        active_order=active,
        has_active_order=has_active,
        last_order_created_at=latest.created_at if latest else None,
        fetched_at=fetched_at,
    )
