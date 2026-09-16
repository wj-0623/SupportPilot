from __future__ import annotations

import hashlib
import json
from time import perf_counter

from sqlalchemy.ext.asyncio import AsyncSession

from app.actions.service import ActionService
from app.agent.graph import SupportGraph
from app.agent.versions import POLICY_VERSION, PROMPT_VERSION, ROUTER_VERSION
from app.core.config import Settings
from app.core.security import Principal
from app.db.models import ActionExecution
from app.db.platform_repository import PlatformRepository
from app.db.repository import ConflictError, NotFoundError, SupportRepository
from app.domain.config import DomainPackConfig
from app.knowledge import partition_fresh_knowledge
from app.metrics import AGENT_RUNS, CHAT_REQUESTS, HANDOFFS
from app.schemas import ChatRequest, ChatResponse, Citation, PendingAction


class SupportService:
    def __init__(
        self, graph: SupportGraph, settings: Settings, action_service: ActionService | None = None
    ) -> None:
        self.graph = graph
        self.settings = settings
        self.action_service = action_service

    async def chat(
        self,
        session: AsyncSession,
        request: ChatRequest,
        idempotency_key: str | None,
        *,
        tenant_id: str,
        customer_id: str,
        channel: str = "web",
    ) -> ChatResponse:
        started = perf_counter()
        repo = SupportRepository(session)
        platform_repo = PlatformRepository(session)
        request_hash = hashlib.sha256(
            json.dumps(
                {
                    "tenant_id": tenant_id,
                    "customer_id": customer_id,
                    "message": request.message,
                    "conversation_id": request.conversation_id,
                    "channel": channel,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        scoped_idempotency_key = f"{tenant_id}:{idempotency_key}" if idempotency_key else None
        conversation_id = request.conversation_id
        try:
            await platform_repo.require_customer(tenant_id, customer_id)
            selected_release = await platform_repo.select_release(tenant_id, customer_id)
            live_pack_revision = (
                await platform_repo.get_domain_pack_revision(
                    tenant_id, selected_release.domain_pack_revision_id
                )
                if selected_release
                else await platform_repo.get_live_domain_pack(tenant_id)
            )
            domain_pack = DomainPackConfig.model_validate_json(live_pack_revision.config_json)
            live_knowledge = await platform_repo.list_live_knowledge(tenant_id)
            live_knowledge, _stale_knowledge = partition_fresh_knowledge(
                live_knowledge, domain_pack.knowledge.freshness_hours
            )
            knowledge_documents = [
                {
                    "id": f"tenant:{item.source_key}:v{item.version}",
                    "title": json.loads(item.metadata_json).get("title", item.source_key),
                    "content": item.content,
                }
                for item in live_knowledge
            ]
            if scoped_idempotency_key:
                cached = await repo.get_idempotent_response(scoped_idempotency_key, request_hash)
                if cached:
                    return ChatResponse.model_validate(cached)

            conversation = await repo.get_or_create_conversation(
                customer_id, request.conversation_id
            )
            await platform_repo.bind_conversation(tenant_id, conversation.id)
            conversation_id = conversation.id
            recent_messages = await repo.recent_messages(conversation.id)
            result = await self.graph.compiled.ainvoke(
                {
                    "tenant_id": tenant_id,
                    "customer_id": customer_id,
                    "domain_pack": domain_pack,
                    "knowledge_documents": knowledge_documents,
                    "conversation_id": conversation.id,
                    "channel": channel,
                    "message": request.message,
                    "history": [
                        {"role": item.role, "content": item.content} for item in recent_messages
                    ],
                    "repo": repo,
                    "trace": [],
                    "observations": [],
                    "token_usage": {},
                }
            )
        except Exception as exc:
            duration_ms = round((perf_counter() - started) * 1_000, 3)
            await session.rollback()
            failure_repo = SupportRepository(session)
            failure_run = await failure_repo.create_agent_run(
                conversation_id=conversation_id,
                customer_id=customer_id,
                message_id=None,
                status="error",
                duration_ms=duration_ms,
                input_hash=request_hash,
                prompt_version=PROMPT_VERSION,
                router_version=ROUTER_VERSION,
                policy_version=POLICY_VERSION,
                model_name=self.settings.openai_model if self.settings.llm_available else None,
                error_type=type(exc).__name__,
            )
            await PlatformRepository(session).bind_run(tenant_id, failure_run.id)
            await session.commit()
            AGENT_RUNS.labels(status="error", intent="unknown", mode="unknown").inc()
            raise
        await repo.add_message(
            conversation.id,
            "user",
            result.get("sanitized_message", request.message),
            metadata={"safety_reasons": result.get("safety_reasons", []), "channel": channel},
        )
        pending_action: PendingAction | None = None
        proposed_action = result.get("proposed_action")
        if proposed_action and self.action_service:
            try:
                action_name = str(proposed_action["action"])
                connector = await platform_repo.find_live_connector(tenant_id, action_name)
                execution = await self._plan_action(
                    session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    pack=domain_pack,
                    connector_id=connector.id,
                    action=action_name,
                    request={
                        key: value for key, value in proposed_action.items() if key != "action"
                    },
                    idempotency_key=f"chat:{conversation.id}:{request_hash[:32]}",
                    conversation_id=conversation.id,
                )
                if not execution.confirmation_digest:
                    raise RuntimeError("Planned action has no confirmation digest")
                pending_action = PendingAction(
                    id=execution.id,
                    action=execution.action,
                    status=execution.status,
                    confirmation_digest=execution.confirmation_digest,
                )
            except (NotFoundError, ConflictError, RuntimeError) as exc:
                result["response"] += " 当前无法创建安全执行计划，已保留本次请求供人工处理。"
                result.setdefault("trace", []).append(
                    f"action:planning_failed:{type(exc).__name__}"
                )
        if (
            result.get("ticket_id")
            and self.action_service
            and domain_pack.handoff.provider != "internal"
        ):
            try:
                connector = await platform_repo.find_live_connector(
                    tenant_id, "handoff", provider=domain_pack.handoff.provider
                )
                await self._plan_action(
                    session,
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    pack=domain_pack,
                    connector_id=connector.id,
                    action="handoff",
                    request={
                        "summary": result.get("ticket_reason") or request.message,
                        "priority": result.get("ticket_priority", "normal"),
                        "custom_attributes": {
                            "supportpilot_ticket_id": result["ticket_id"],
                            "supportpilot_conversation_id": conversation.id,
                        },
                    },
                    idempotency_key=f"handoff:{result['ticket_id']}",
                    conversation_id=conversation.id,
                )
                result.setdefault("trace", []).append("handoff:connector_queued")
            except (NotFoundError, ConflictError, RuntimeError) as exc:
                result.setdefault("trace", []).append(
                    f"handoff:connector_failed:{type(exc).__name__}"
                )
        metadata = {
            "mode": result["mode"],
            "citations": result.get("citations", []),
            "trace": result.get("trace", []),
            "ticket_id": result.get("ticket_id"),
            "pending_action_id": pending_action.id if pending_action else None,
        }
        assistant_message = await repo.add_message(
            conversation.id,
            "assistant",
            result["response"],
            intent=result["intent"],
            metadata=metadata,
        )
        duration_ms = round((perf_counter() - started) * 1_000, 3)
        tool_calls = 0
        for trace_item in result.get("trace", []):
            if trace_item.startswith("agent:tool_steps:"):
                tool_calls = int(trace_item.rsplit(":", maxsplit=1)[-1])
        token_usage = result.get("token_usage", {})
        run = await repo.create_agent_run(
            conversation_id=conversation.id,
            customer_id=customer_id,
            message_id=assistant_message.id,
            status="success",
            intent=result["intent"],
            mode=result["mode"],
            route_confidence=result.get("confidence"),
            duration_ms=duration_ms,
            tool_calls=tool_calls,
            input_tokens=token_usage.get("input_tokens", 0),
            output_tokens=token_usage.get("output_tokens", 0),
            handoff=bool(result.get("ticket_id")),
            citation_count=len(result.get("citations", [])),
            input_hash=request_hash,
            prompt_version=PROMPT_VERSION,
            router_version=ROUTER_VERSION,
            policy_version=POLICY_VERSION,
            model_name=self.settings.openai_model if result["mode"] == "agent" else None,
            safety_flags=result.get("safety_reasons", []),
            trace=result.get("trace", []),
            observations=result.get("observations", []),
        )
        await platform_repo.bind_run(
            tenant_id, run.id, selected_release.id if selected_release else None
        )
        await platform_repo.save_checkpoint(
            tenant_id,
            conversation.id,
            "completed",
            {
                "run_id": run.id,
                "intent": result["intent"],
                "mode": result["mode"],
                "ticket_id": result.get("ticket_id"),
            },
        )
        metadata["run_id"] = run.id
        assistant_message.metadata_json = json.dumps(metadata, ensure_ascii=False)
        response = ChatResponse(
            conversation_id=conversation.id,
            message_id=assistant_message.id,
            reply=result["response"],
            intent=result["intent"],
            mode=result["mode"],
            citations=[Citation.model_validate(item) for item in result.get("citations", [])],
            ticket_id=result.get("ticket_id"),
            pending_action=pending_action,
            trace=result.get("trace", []) if self.settings.expose_debug_trace else [],
        )
        if scoped_idempotency_key:
            await repo.save_idempotent_response(
                scoped_idempotency_key, request_hash, json.loads(response.model_dump_json())
            )
        await session.commit()
        CHAT_REQUESTS.labels(intent=response.intent, mode=response.mode).inc()
        AGENT_RUNS.labels(status="success", intent=response.intent, mode=response.mode).inc()
        if response.ticket_id:
            HANDOFFS.labels(priority=result.get("ticket_priority", "normal")).inc()
        return response

    async def _plan_action(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        customer_id: str,
        pack: DomainPackConfig,
        connector_id: str,
        action: str,
        request: dict[str, object],
        idempotency_key: str,
        conversation_id: str,
    ) -> ActionExecution:
        if self.action_service is None:
            raise RuntimeError("Action service is unavailable")
        return await self.action_service.plan(
            session,
            principal=Principal(
                subject=f"customer:{customer_id}",
                tenant_id=tenant_id,
                role="customer",
                customer_id=customer_id,
            ),
            pack=pack,
            connector_id=connector_id,
            action=action,
            request=request,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
        )
