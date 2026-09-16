from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.connectors.base import ConnectorError, ConnectorResult


class MockCommerceProvider:
    """Deterministic commerce sandbox used by local demos, simulations and CI."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {
            "ORD-1001": {"id": "ORD-1001", "status": "delivered", "customer_id": "demo-001"},
            "ORD-1002": {"id": "ORD-1002", "status": "shipped", "customer_id": "demo-001"},
            "ORD-2001": {"id": "ORD-2001", "status": "processing", "customer_id": "demo-002"},
        }
        self._results: dict[str, ConnectorResult] = {}

    async def healthcheck(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ConnectorResult:
        if idempotency_key in self._results:
            return self._results[idempotency_key]
        handler = getattr(self, f"_handle_{action}", None)
        if handler is None:
            raise ConnectorError("unsupported_action", f"Mock does not support {action}")
        result: ConnectorResult = handler(payload)
        self._results[idempotency_key] = result
        return result

    def _owned_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        order = self.orders.get(str(payload.get("order_id")))
        if not order or order["customer_id"] != payload.get("customer_id"):
            raise ConnectorError("order_not_found", "Order not found")
        return order

    def _handle_get_order(self, payload: dict[str, Any]) -> ConnectorResult:
        return ConnectorResult(deepcopy(self._owned_order(payload)))

    def _handle_list_orders(self, payload: dict[str, Any]) -> ConnectorResult:
        orders = [
            deepcopy(order)
            for order in self.orders.values()
            if order["customer_id"] == payload.get("customer_id")
        ]
        return ConnectorResult({"orders": orders})

    def _handle_search_products(self, payload: dict[str, Any]) -> ConnectorResult:
        return ConnectorResult({"products": [], "query": payload.get("query", "")})

    def _handle_check_inventory(self, payload: dict[str, Any]) -> ConnectorResult:
        return ConnectorResult({"sku": payload.get("sku"), "available": True, "quantity": 10})

    def _handle_cancel_order(self, payload: dict[str, Any]) -> ConnectorResult:
        order = self._owned_order(payload)
        if order["status"] != "processing":
            raise ConnectorError("invalid_order_state", "Only processing orders can be cancelled")
        order["status"] = "cancelled"
        return ConnectorResult(deepcopy(order), external_id=order["id"])

    def _handle_change_address(self, payload: dict[str, Any]) -> ConnectorResult:
        order = self._owned_order(payload)
        if order["status"] != "processing":
            raise ConnectorError("invalid_order_state", "Address can no longer be changed")
        order["shipping_address"] = payload.get("shipping_address")
        return ConnectorResult(deepcopy(order), external_id=order["id"])

    def _handle_create_return(self, payload: dict[str, Any]) -> ConnectorResult:
        order = self._owned_order(payload)
        if order["status"] != "delivered":
            raise ConnectorError("invalid_order_state", "Only delivered orders can be returned")
        return ConnectorResult(
            {"return_id": f"RET-{order['id']}", "status": "requested"},
            external_id=f"RET-{order['id']}",
        )

    def _handle_exchange_item(self, payload: dict[str, Any]) -> ConnectorResult:
        return self._handle_create_return(payload)

    def _handle_handoff(self, payload: dict[str, Any]) -> ConnectorResult:
        return ConnectorResult({"status": "queued", "reason": payload.get("reason")})
