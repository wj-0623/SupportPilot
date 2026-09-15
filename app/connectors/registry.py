from __future__ import annotations

import json
from typing import Any

from app.connectors.base import CommerceProvider
from app.connectors.chatwoot import ChatwootHelpdeskProvider
from app.connectors.credentials import CredentialResolver
from app.connectors.generic_rest import GenericRestCommerceProvider, RestAction
from app.connectors.mock import MockCommerceProvider
from app.connectors.shopify import ShopifyCommerceProvider
from app.db.models import ConnectorDefinition


class ProviderRegistry:
    def __init__(self, credential_resolver: CredentialResolver) -> None:
        self.credential_resolver = credential_resolver
        self.mock = MockCommerceProvider()

    def build(
        self,
        connector: ConnectorDefinition,
        *,
        allowed_hosts: list[str],
        timeout_seconds: float,
        max_retries: int,
    ) -> CommerceProvider:
        config: dict[str, Any] = json.loads(connector.config_json)
        if connector.provider == "mock":
            return self.mock
        if not connector.credential_ref:
            raise ValueError("enabled external connectors require a credential_ref")
        token = self.credential_resolver.resolve(connector.credential_ref)
        if connector.provider == "generic-rest":
            if not connector.base_url:
                raise ValueError("generic REST connector requires base_url")
            actions = {
                name: RestAction(method=spec["method"], path=spec["path"])
                for name, spec in config.get("actions", {}).items()
            }
            return GenericRestCommerceProvider(
                base_url=connector.base_url,
                token=token,
                actions=actions,
                allowed_hosts=allowed_hosts,
                timeout_seconds=timeout_seconds,
                max_retries=max_retries,
            )
        if connector.provider == "shopify":
            if not connector.base_url:
                raise ValueError("Shopify connector requires the shop origin")
            return ShopifyCommerceProvider(
                shop_origin=connector.base_url,
                access_token=token,
                allowed_hosts=allowed_hosts,
                api_version=str(config.get("api_version", "2026-07")),
                timeout_seconds=timeout_seconds,
            )
        if connector.provider == "chatwoot":
            if not connector.base_url:
                raise ValueError("Chatwoot connector requires base_url")
            return ChatwootHelpdeskProvider(
                base_url=connector.base_url,
                api_token=token,
                account_id=int(config["account_id"]),
                inbox_id=int(config["inbox_id"]),
                allowed_hosts=allowed_hosts,
                timeout_seconds=timeout_seconds,
            )
        raise ValueError(f"unsupported connector provider: {connector.provider}")
