from __future__ import annotations

from typing import Any

import httpx

from app.connectors.base import ConnectorError, ConnectorResult
from app.connectors.generic_rest import validate_connector_url


class ShopifyCommerceProvider:
    """Reference adapter for Shopify GraphQL Admin API 2026-07."""

    def __init__(
        self,
        *,
        shop_origin: str,
        access_token: str,
        allowed_hosts: list[str],
        api_version: str = "2026-07",
        timeout_seconds: float = 10,
    ) -> None:
        origin = validate_connector_url(shop_origin, allowed_hosts)
        self.endpoint = f"{origin}admin/api/{api_version}/graphql.json"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            headers={"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"},
            follow_redirects=False,
        )

    async def healthcheck(self) -> bool:
        try:
            result = await self._graphql("query ShopSageHealth { shop { id } }")
            return "shop" in result
        except ConnectorError:
            return False

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self.client.post(
                self.endpoint, json={"query": query, "variables": variables or {}}
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(
                "shopify_network", "Shopify request failed", retryable=True
            ) from exc
        if body.get("errors"):
            raise ConnectorError("shopify_graphql", "Shopify rejected the GraphQL operation")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ConnectorError("shopify_response", "Shopify returned no data")
        return data

    async def execute(
        self,
        action: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> ConnectorResult:
        if action == "get_order":
            external_customer_id = str(payload.get("external_customer_id", ""))
            if not external_customer_id:
                raise ConnectorError(
                    "missing_external_identity",
                    "Shopify customer mapping is required before accessing an order",
                )
            customer_search_id = external_customer_id.rsplit("/", 1)[-1]
            query = """
            query ShopSageOrder($query: String!) {
              orders(first: 1, query: $query) {
                nodes { id name displayFinancialStatus displayFulfillmentStatus
                  customer { id } fulfillments(first: 5) { trackingInfo { number url } } }
              }
            }
            """
            search = f"name:{payload['order_id']} customer_id:{customer_search_id}"
            data = await self._graphql(query, {"query": search})
            nodes = data.get("orders", {}).get("nodes", [])
            if not nodes:
                raise ConnectorError("order_not_found", "Order not found")
            order = nodes[0]
            returned_customer_id = str((order.get("customer") or {}).get("id", ""))
            if returned_customer_id.rsplit("/", 1)[-1] != customer_search_id:
                raise ConnectorError("order_not_found", "Order not found")
            fulfillment = str(order.get("displayFulfillmentStatus") or "").upper()
            status_map = {
                "UNFULFILLED": "processing",
                "SCHEDULED": "processing",
                "ON_HOLD": "processing",
                "IN_PROGRESS": "shipped",
                "PARTIALLY_FULFILLED": "shipped",
                "FULFILLED": "delivered",
            }
            return ConnectorResult(
                {
                    "id": order.get("name"),
                    "status": status_map.get(fulfillment, "unknown"),
                    "financial_status": order.get("displayFinancialStatus"),
                    "fulfillment_status": fulfillment,
                    "fulfillments": order.get("fulfillments", []),
                }
            )
        if action == "create_return":
            mutation = """
            mutation ShopSageReturn($input: ReturnInput!) {
              returnCreate(returnInput: $input) {
                return { id status }
                userErrors { field message }
              }
            }
            """
            data = await self._graphql(mutation, {"input": payload["return_input"]})
            result = data["returnCreate"]
            if result.get("userErrors"):
                raise ConnectorError("shopify_validation", "Shopify rejected the return request")
            return ConnectorResult(result, external_id=result["return"]["id"])
        raise ConnectorError("unsupported_action", f"Shopify adapter does not support {action}")

    async def close(self) -> None:
        await self.client.aclose()
