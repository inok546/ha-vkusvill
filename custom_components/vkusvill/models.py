"""Order models and evidence-based status selection."""

from collections.abc import Iterable, Mapping
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


@dataclass(frozen=True)
class OrderStatus:
    """Normalized stage and activity evidenced by the provider's status."""

    stage: Stage
    active: bool | None
    delivery_stage: Stage | None = None


# Add exact values only with a source in docs/API_FINDINGS.md.
VERIFIED_STATUSES: Mapping[str, OrderStatus] = {
    "Сборка": OrderStatus(Stage.ASSEMBLING, True),
    "Готов": OrderStatus(Stage.OTHER, True),
    "Доставка": OrderStatus(Stage.WITH_COURIER, True),
    "Успешно завершен": OrderStatus(Stage.OTHER, False, delivery_stage=Stage.DELIVERED),
}
UNKNOWN_STATUS = OrderStatus(Stage.UNKNOWN, None)


@dataclass(frozen=True)
class Order:
    """Minimal fields required for selection and entity state."""

    key: str | None
    number: str | None
    status_name: str | None
    created_at: datetime | None
    is_delivery: bool = False

    @property
    def status(self) -> OrderStatus:
        """Look up the exact status reported by MCP."""
        return (
            VERIFIED_STATUSES.get(self.status_name, UNKNOWN_STATUS)
            if self.status_name
            else UNKNOWN_STATUS
        )

    @property
    def stage(self) -> Stage:
        """Classify an exact, verified status."""
        status = self.status
        if self.is_delivery and status.delivery_stage is not None:
            return status.delivery_stage
        return status.stage


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


def newest_order(orders: Iterable[Order]) -> Order | None:
    """Choose the newest order with deterministic tie-breaking."""
    return max(orders, key=_sort_key, default=None)


def select_orders(history: OrderHistory, fetched_at: datetime) -> OrderSnapshot:
    """Prefer the newest verified active order, falling back to history."""
    orders = history.orders
    active = newest_order(order for order in orders if order.status.active is True)
    has_active = None
    if active is not None:
        has_active = True
    elif history.complete and orders and all(order.status.active is False for order in orders):
        has_active = False
    latest = newest_order(orders)
    return OrderSnapshot(
        history=history,
        selected=active or latest,
        active_order=active,
        has_active_order=has_active,
        last_order_created_at=latest.created_at if latest else None,
        fetched_at=fetched_at,
    )
