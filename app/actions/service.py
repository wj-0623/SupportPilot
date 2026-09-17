from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError
from app.connectors.registry import ProviderRegistry
from app.core.config import Settings
from app.core.security import Principal
from app.db.models import ActionExecution
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError
from app.domain.config import DomainPackConfig
from app.metrics import ACTION_EXECUTIONS, CONNECTOR_LATENCY

TERMINAL_ACTION_STATES = {"succeeded", "failed", "cancelled"}
tracer = trace.get_tracer(__name__)
logger = logging.getLogger(__name__)


def action_digest(action: str, customer_id: str, request: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"action": action, "customer_id": customer_id, "request": request},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionService:
    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers

    async def plan(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        pack: DomainPackConfig,
        connector_id: str,
        action: str,
        request: dict[str, Any],
        idempotency_key: str,
        conversation_id: str | None,
    ) -> ActionExecution:
        repo = PlatformRepository(session)
        permission = pack.tool_permission(action)
        if permission is None or not permission.enabled:
            raise ConflictError("Action is disabled by the live Domain Pack")
        if principal.role not in permission.roles:
            raise ConflictError("Principal is not allowed to request this action")
        customer_id = principal.customer_id or str(request.get("customer_id", ""))
        if not customer_id:
            raise ConflictError("A customer context is required")
        customer_link = await repo.require_customer(principal.tenant_id, customer_id)
        connector = await repo.get_connector(principal.tenant_id, connector_id)
        declared_capabilities = set(json.loads(connector.capabilities_json))
        if connector.status != "live" or action not in declared_capabilities:
            raise ConflictError("Connector is not live or does not declare this action")
        if action not in self.providers.supported_capabilities(connector):
            raise ConflictError("Connector adapter does not implement this action")
        existing = await repo.get_action_by_key(principal.tenant_id, idempotency_key)
        if existing:
            expected = action_digest(action, customer_id, request)
            if existing.confirmation_digest != expected:
                raise ConflictError("Action idempotency key was reused with another request")
            return existing
        digest = action_digest(action, customer_id, request)
        requires_human = action in pack.policies.human_approval_actions
        requires_customer = (
            permission.risk in {"medium", "high"}
            or action in pack.policies.customer_confirmation_actions
        )
        if requires_human:
            initial_status = "awaiting_human_approval"
        elif requires_customer:
            initial_status = "awaiting_customer_confirmation"
        else:
            initial_status = "queued"
        execution = await repo.create_action(
            tenant_id=principal.tenant_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            connector_id=connector_id,
            action=action,
            risk_tier=permission.risk,
            idempotency_key=idempotency_key,
            request={
                **request,
                "customer_id": customer_id,
                "external_customer_id": customer_link.external_id or customer_id,
            },
            status=initial_status,
            confirmation_digest=digest,
        )
        await repo.append_outbox(
            principal.tenant_id,
            execution.id,
            "action.planned",
            {"action_id": execution.id, "status": initial_status, "action": action},
        )
        return execution

    async def confirm(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        action_id: str,
        digest: str,
    ) -> ActionExecution:
        principal.require_roles("customer")
        repo = PlatformRepository(session)
        execution = await repo.get_action(principal.tenant_id, action_id)
        if execution.status != "awaiting_customer_confirmation":
            raise ConflictError("Action is not waiting for customer confirmation")
        if principal.role == "customer" and execution.customer_id != principal.customer_id:
            raise ConflictError("Action does not belong to this customer")
        if not execution.confirmation_digest or not hmac.compare_digest(
            execution.confirmation_digest, digest
        ):
            raise ConflictError("Action details changed; request a new confirmation")
        execution.status = "queued"
        execution.confirmed_by = principal.subject
        execution.confirmed_at = datetime.now(UTC)
        await repo.append_outbox(
            principal.tenant_id,
            execution.id,
            "action.confirmed",
            {"action_id": execution.id},
        )
        return execution

    async def approve(
        self, session: AsyncSession, *, principal: Principal, action_id: str
    ) -> ActionExecution:
        principal.require_roles("agent", "admin")
        repo = PlatformRepository(session)
        execution = await repo.get_action(principal.tenant_id, action_id)
        if execution.status != "awaiting_human_approval":
            raise ConflictError("Action is not waiting for human approval")
        execution.status = "queued"
        execution.confirmed_by = principal.subject
        execution.confirmed_at = datetime.now(UTC)
        await repo.append_outbox(
            principal.tenant_id,
            execution.id,
            "action.approved",
            {"action_id": execution.id, "approver": principal.subject},
        )
        return execution

    async def execute_queued(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        action_id: str,
        pack: DomainPackConfig,
    ) -> ActionExecution:
        repo = PlatformRepository(session)
        execution = await repo.get_action(tenant_id, action_id)
        if execution.status in TERMINAL_ACTION_STATES:
            return execution
        if execution.status not in {"queued", "retryable"}:
            raise ConflictError("Action is not ready to execute")
        now = datetime.now(UTC)
        if execution.next_attempt_at and execution.next_attempt_at > now:
            raise ConflictError("Action retry is not due yet")
        connector = await repo.get_connector(tenant_id, execution.connector_id)
        provider = None
        execution.status = "executing"
        execution.attempt_count += 1
        execution.next_attempt_at = None
        await session.flush()
        request = json.loads(execution.request_json)
        started = perf_counter()
        try:
            if connector.status != "live":
                raise ConnectorError("connector_not_live", "Connector is not live", retryable=False)
            provider = self.providers.build(
                connector,
                allowed_hosts=pack.safety.allowed_connector_hosts,
                timeout_seconds=self.settings.connector_timeout_seconds,
                max_retries=self.settings.connector_max_retries,
            )
            with tracer.start_as_current_span("commerce.connector.execute") as span:
                span.set_attribute("commerce.provider", connector.provider)
                span.set_attribute("commerce.action", execution.action)
                span.set_attribute("commerce.attempt", execution.attempt_count)
                await self._revalidate_state(
                    provider,
                    execution.action,
                    request,
                    pack,
                    idempotency_scope=f"{tenant_id}:{execution.id}",
                )
                result = await provider.execute(
                    execution.action,
                    request,
                    idempotency_key=f"{tenant_id}:{execution.idempotency_key}",
                )
            execution.result_json = json.dumps(result.data, ensure_ascii=False)
            execution.status = "succeeded"
            ACTION_EXECUTIONS.labels(
                action=execution.action, status="succeeded", provider=connector.provider
            ).inc()
            await repo.append_outbox(
                tenant_id,
                execution.id,
                "action.succeeded",
                {"action_id": execution.id, "external_id": result.external_id},
            )
        except ConnectorError as exc:
            await self._record_failure(
                repo,
                tenant_id,
                execution,
                provider_name=connector.provider,
                code=exc.code,
                retryable=exc.retryable,
            )
        except Exception as exc:
            logger.exception(
                "Connector setup or execution failed unexpectedly",
                extra={"provider": connector.provider, "action": execution.action},
            )
            await self._record_failure(
                repo,
                tenant_id,
                execution,
                provider_name=connector.provider,
                code=type(exc).__name__,
                retryable=False,
            )
        finally:
            CONNECTOR_LATENCY.labels(action=execution.action, provider=connector.provider).observe(
                perf_counter() - started
            )
            if provider is not None:
                try:
                    await provider.close()
                except Exception:
                    logger.exception(
                        "Failed to close connector provider",
                        extra={"provider": connector.provider},
                    )
        return execution

    async def _record_failure(
        self,
        repo: PlatformRepository,
        tenant_id: str,
        execution: ActionExecution,
        *,
        provider_name: str,
        code: str,
        retryable: bool,
    ) -> None:
        execution.error_code = code[:100]
        can_retry = retryable and execution.attempt_count < self.settings.action_max_attempts
        execution.status = "retryable" if can_retry else "failed"
        if can_retry:
            delay_seconds = min(300, 2**execution.attempt_count)
            execution.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay_seconds)
        ACTION_EXECUTIONS.labels(
            action=execution.action, status=execution.status, provider=provider_name
        ).inc()
        await repo.append_outbox(
            tenant_id,
            execution.id,
            "action.failed",
            {"action_id": execution.id, "code": code, "retryable": can_retry},
        )

    async def _revalidate_state(
        self,
        provider: Any,
        action: str,
        request: dict[str, Any],
        pack: DomainPackConfig,
        *,
        idempotency_scope: str,
    ) -> None:
        if action not in {"cancel_order", "change_address", "create_return", "exchange_item"}:
            return
        result = await provider.execute(
            "get_order",
            request,
            idempotency_key=f"preflight:{idempotency_scope}:{request.get('order_id')}",
        )
        status = result.data.get("status")
        if action == "cancel_order" and status not in pack.policies.cancellations_allowed_statuses:
            raise ConnectorError("state_changed", "Order can no longer be cancelled")
        if (
            action == "change_address"
            and status not in pack.policies.address_change_allowed_statuses
        ):
            raise ConnectorError("state_changed", "Address can no longer be changed")
        if action in {"create_return", "exchange_item"} and status != "delivered":
            raise ConnectorError("state_changed", "Order is not eligible for return")
