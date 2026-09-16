from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from app.agent.state import SupportState
from app.agent.versions import SYSTEM_PROMPT
from app.core.config import Settings
from app.db.models import Order
from app.db.repository import NotFoundError
from app.domain.guardrails import inspect_message
from app.domain.intents import classify_intent, classify_intents, extract_order_id
from app.domain.policies import evaluate_return
from app.knowledge import KnowledgeBase, KnowledgeHit
from app.metrics import KNOWLEDGE_RETRIEVALS, NODE_LATENCY, TOOL_CALLS

logger = logging.getLogger(__name__)


STATUS_LABELS = {
    "processing": "处理中",
    "shipped": "已发货",
    "delivered": "已签收",
    "cancelled": "已取消",
}


def _order_payload(order: Order) -> dict[str, str | None]:
    return {
        "order_id": order.id,
        "status": STATUS_LABELS.get(order.status, order.status),
        "product": order.product_name,
        "amount": f"¥{Decimal(order.total_amount):.2f}",
        "tracking_number": order.tracking_number,
        "created_at": order.created_at.isoformat(),
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
    }


def _hits_payload(hits: list[KnowledgeHit]) -> list[dict[str, str | float]]:
    return [
        {
            "source_id": hit.source_id,
            "title": hit.title,
            "content": hit.content,
            "score": hit.score,
        }
        for hit in hits
    ]


class SupportGraph:
    def __init__(self, settings: Settings, knowledge_base: KnowledgeBase) -> None:
        self.settings = settings
        self.knowledge_base = knowledge_base
        self.compiled = self._build()

    def _search(self, state: SupportState, query: str, limit: int) -> list[KnowledgeHit]:
        pack = state.get("domain_pack")
        configured_limit = pack.knowledge.max_hits if pack else limit
        hits = self.knowledge_base.search_with_tenant_documents(
            query,
            state.get("knowledge_documents", []),
            limit=min(limit, configured_limit),
        )
        if pack:
            hits = [hit for hit in hits if hit.score >= pack.knowledge.minimum_score]
        KNOWLEDGE_RETRIEVALS.labels(grounded=str(bool(hits)).lower()).inc()
        return hits

    def _build(self):  # type: ignore[no-untyped-def]
        graph = StateGraph(SupportState)
        graph.add_node("guard", cast(Any, self._observed("guard", self.guard)))
        graph.add_node("classify", cast(Any, self._observed("classify", self.classify)))
        graph.add_node("safety_reply", cast(Any, self._observed("safety_reply", self.safety_reply)))
        graph.add_node(
            "order_workflow", cast(Any, self._observed("order_workflow", self.order_workflow))
        )
        graph.add_node(
            "return_workflow", cast(Any, self._observed("return_workflow", self.return_workflow))
        )
        graph.add_node(
            "knowledge_workflow",
            cast(Any, self._observed("knowledge_workflow", self.knowledge_workflow)),
        )
        graph.add_node(
            "dynamic_agent", cast(Any, self._observed("dynamic_agent", self.dynamic_agent))
        )
        graph.add_node(
            "human_handoff", cast(Any, self._observed("human_handoff", self.human_handoff))
        )
        graph.add_node(
            "multi_intent_workflow",
            cast(Any, self._observed("multi_intent_workflow", self.multi_intent_workflow)),
        )
        graph.add_node(
            "change_order_workflow",
            cast(Any, self._observed("change_order_workflow", self.change_order_workflow)),
        )

        graph.add_edge(START, "guard")
        graph.add_conditional_edges(
            "guard", self.after_guard, {"safe": "classify", "blocked": "safety_reply"}
        )
        graph.add_conditional_edges(
            "classify",
            self.after_classify,
            {
                "order_status": "order_workflow",
                "return_refund": "return_workflow",
                "knowledge": "knowledge_workflow",
                "human_handoff": "human_handoff",
                "general": "dynamic_agent",
                "multi": "multi_intent_workflow",
                "cancel_or_change": "change_order_workflow",
            },
        )
        graph.add_conditional_edges(
            "return_workflow",
            self.after_business_workflow,
            {"handoff": "human_handoff", "done": END},
        )
        graph.add_conditional_edges(
            "dynamic_agent",
            self.after_business_workflow,
            {"handoff": "human_handoff", "done": END},
        )
        graph.add_edge("safety_reply", END)
        graph.add_edge("order_workflow", END)
        graph.add_edge("knowledge_workflow", END)
        graph.add_edge("human_handoff", END)
        graph.add_edge("multi_intent_workflow", END)
        graph.add_edge("change_order_workflow", END)
        return graph.compile()

    @staticmethod
    def _observed(
        name: str, handler: Callable[[SupportState], Awaitable[dict[str, Any]]]
    ) -> Callable[[SupportState], Awaitable[dict[str, Any]]]:
        async def wrapped(state: SupportState) -> dict[str, Any]:
            started = perf_counter()
            tracer = trace.get_tracer("supportpilot.agent")
            with tracer.start_as_current_span(f"agent.node.{name}") as span:
                try:
                    result = await handler(state)
                except Exception as exc:
                    elapsed_ms = round((perf_counter() - started) * 1_000, 3)
                    NODE_LATENCY.labels(node=name).observe(elapsed_ms / 1_000)
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                    raise

                elapsed_ms = round((perf_counter() - started) * 1_000, 3)
                attributes: dict[str, str | bool | int | float] = {
                    key: result[key]
                    for key in ("intent", "mode", "needs_human")
                    if key in result and isinstance(result[key], (str, bool, int, float))
                }
                attributes["citation_count"] = len(result.get("citations", []))
                observation: dict[str, Any] = {
                    "node": name,
                    "status": "ok",
                    "duration_ms": elapsed_ms,
                    "attributes": attributes,
                }
                NODE_LATENCY.labels(node=name).observe(elapsed_ms / 1_000)
                span.set_attribute("supportpilot.node.name", name)
                span.set_attribute("supportpilot.node.duration_ms", elapsed_ms)
                for key, value in attributes.items():
                    span.set_attribute(f"supportpilot.{key}", value)
                result["observations"] = [observation]
                return result

        return wrapped

    async def guard(self, state: SupportState) -> dict:
        result = inspect_message(state["message"])
        return {
            "sanitized_message": result.sanitized_text,
            "safe_for_agent": result.safe_for_agent,
            "safety_reasons": list(result.reasons),
            "trace": ["guard:checked"],
        }

    @staticmethod
    def after_guard(state: SupportState) -> Literal["safe", "blocked"]:
        return "safe" if state["safe_for_agent"] else "blocked"

    async def classify(self, state: SupportState) -> dict:
        pack = state.get("domain_pack")
        patterns = pack.intent_patterns() if pack else None
        if pack and pack.handoff.enabled:
            patterns = dict(pack.intent_patterns())
            existing = patterns.get("human_handoff", ())
            patterns["human_handoff"] = tuple(
                dict.fromkeys((*existing, *pack.handoff.escalation_keywords))
            )
        result = classify_intent(state["sanitized_message"], patterns)
        results = classify_intents(
            state["sanitized_message"],
            patterns,
            limit=pack.safety.max_intents_per_turn if pack else 3,
        )
        change_intent = next((item for item in results if item.name == "cancel_or_change"), None)
        if change_intent:
            result = change_intent
            results = [change_intent]
        order_id = extract_order_id(state["sanitized_message"])
        if result.name == "general" and order_id:
            result = classify_intent("订单", patterns)
        order_id_inferred = False
        if not order_id:
            for message in reversed(state.get("history", [])):
                order_id = extract_order_id(message["content"])
                if order_id:
                    order_id_inferred = True
                    break
        return {
            "intent": result.name,
            "intents": [item.name for item in results],
            "confidence": result.confidence,
            "order_id": order_id,
            "order_id_inferred": order_id_inferred,
            "trace": [
                f"route:{result.name}:{result.confidence:.2f}",
                *(["context:order_id_inferred"] if order_id_inferred else []),
            ],
        }

    @staticmethod
    def after_classify(
        state: SupportState,
    ) -> Literal[
        "order_status",
        "return_refund",
        "knowledge",
        "human_handoff",
        "general",
        "multi",
        "cancel_or_change",
    ]:
        if len(state.get("intents", [])) > 1:
            return "multi"
        intent = state["intent"]
        if intent in {"faq", "product"}:
            return "knowledge"
        if intent in {"order_status", "return_refund", "human_handoff"}:
            return intent  # type: ignore[return-value]
        if intent == "cancel_or_change":
            return "cancel_or_change"
        return "general"

    async def change_order_workflow(self, state: SupportState) -> dict[str, Any]:
        order_id = state.get("order_id")
        if not order_id:
            return {
                "response": "请提供要取消或修改的订单号。",
                "mode": "workflow",
                "citations": [],
                "trace": ["action:missing_order_id"],
            }
        try:
            order = await state["repo"].get_order(state["customer_id"], order_id)
        except NotFoundError:
            return {
                "response": "没有找到该账户名下的订单。",
                "mode": "workflow",
                "citations": [],
                "trace": ["action:order_not_found_or_not_owned"],
            }
        normalized = state["sanitized_message"].lower()
        if any(item in normalized for item in ("取消", "cancel")):
            pack = state.get("domain_pack")
            allowed = pack.policies.cancellations_allowed_statuses if pack else ["processing"]
            if order.status not in allowed:
                status_label = STATUS_LABELS.get(order.status, order.status)
                return {
                    "response": (
                        f"订单 {order.id} 当前为{status_label}，已不能自动取消；"
                        "我可以为你转人工核验。"
                    ),
                    "mode": "workflow",
                    "citations": [],
                    "trace": ["action:cancel_policy_denied"],
                }
            return {
                "response": (f"订单 {order.id} 当前仍可取消。请确认后再执行，取消成功后无法恢复。"),
                "mode": "workflow",
                "citations": [],
                "proposed_action": {"action": "cancel_order", "order_id": order.id},
                "trace": ["action:cancel_plan_ready"],
            }
        return {
            "response": (f"订单 {order.id} 的改址需要完整新地址。请在安全表单中填写后确认执行。"),
            "mode": "workflow",
            "citations": [],
            "trace": ["action:address_details_required"],
        }

    async def multi_intent_workflow(self, state: SupportState) -> dict[str, Any]:
        replies: list[str] = []
        citations: dict[str, dict[str, str]] = {}
        traces: list[str] = [f"multi:intents:{','.join(state.get('intents', []))}"]
        ticket_id: str | None = None
        for intent in state.get("intents", []):
            if intent == "order_status":
                result = await self.order_workflow(state)
            elif intent == "return_refund":
                result = await self.return_workflow(state)
            elif intent in {"faq", "product"}:
                result = await self.knowledge_workflow(state)
            elif intent == "human_handoff":
                result = await self.human_handoff(state)
            elif intent == "cancel_or_change":
                result = await self.change_order_workflow(state)
            else:
                continue
            reply = str(result.get("response", "")).strip()
            if reply:
                replies.append(reply)
            for citation in result.get("citations", []):
                citations[citation["source_id"]] = citation
            traces.extend(result.get("trace", []))
            ticket_id = result.get("ticket_id") or ticket_id
            if result.get("needs_human"):
                handoff = await self.human_handoff(cast(SupportState, {**state, **result}))
                replies.append(str(handoff["response"]))
                traces.extend(handoff.get("trace", []))
                ticket_id = handoff.get("ticket_id") or ticket_id
        if not replies:
            return await self.dynamic_agent(state)
        return {
            "response": "\n\n".join(replies),
            "mode": "workflow",
            "citations": list(citations.values()),
            "ticket_id": ticket_id,
            "trace": traces,
        }

    @staticmethod
    def after_business_workflow(state: SupportState) -> Literal["handoff", "done"]:
        return "handoff" if state.get("needs_human") else "done"

    async def safety_reply(self, state: SupportState) -> dict:
        return {
            "intent": "safety",
            "mode": "workflow",
            "response": (
                "我不能显示或更改系统内部指令。我仍然可以帮你查询本人订单、商品、配送、"
                "保修或退货政策；请直接告诉我需要处理的电商问题。"
            ),
            "citations": [],
            "trace": ["guard:agent_blocked"],
        }

    async def order_workflow(self, state: SupportState) -> dict:
        repo = state["repo"]
        order_id = state.get("order_id")
        if not order_id:
            orders = await repo.list_orders(state["customer_id"])
            if not orders:
                response = "当前账户下没有可查询的订单。"
            else:
                summary = "；".join(
                    (
                        f"{order.id}（{order.product_name}，"
                        f"{STATUS_LABELS.get(order.status, order.status)}）"
                    )
                    for order in orders
                )
                response = f"请提供要查询的订单号。你最近的订单有：{summary}。"
            return {
                "response": response,
                "mode": "workflow",
                "citations": [],
                "trace": ["order:request_order_id"],
            }
        try:
            order = await repo.get_order(state["customer_id"], order_id)
        except NotFoundError:
            return {
                "response": "没有找到该账户名下的订单。请检查订单号，或要求转人工核验。",
                "mode": "workflow",
                "citations": [],
                "trace": ["order:not_found_or_not_owned"],
            }
        tracking = f"，物流单号 {order.tracking_number}" if order.tracking_number else ""
        response = (
            f"订单 {order.id} 的商品是“{order.product_name}”，当前状态："
            f"{STATUS_LABELS.get(order.status, order.status)}{tracking}。"
        )
        return {
            "response": response,
            "mode": "workflow",
            "citations": [],
            "trace": ["order:ownership_verified", "order:status_read"],
        }

    async def return_workflow(self, state: SupportState) -> dict:
        repo = state["repo"]
        order_id = state.get("order_id")
        policy_citation = [{"source_id": "policy:return-30d", "title": "30 天退货政策"}]
        if not order_id:
            return {
                "response": "请提供需要退货或退款的订单号，例如 ORD-1001。",
                "mode": "workflow",
                "citations": policy_citation,
                "needs_human": False,
                "trace": ["return:request_order_id"],
            }
        try:
            order = await repo.get_order(state["customer_id"], order_id)
        except NotFoundError:
            return {
                "response": "没有找到该账户名下的订单，无法继续退货审核。",
                "mode": "workflow",
                "citations": policy_citation,
                "needs_human": False,
                "trace": ["return:ownership_failed"],
            }
        pack = state.get("domain_pack")
        return_policy = pack.policies.returns if pack else None
        decision = evaluate_return(
            order,
            window_days=return_policy.window_days if return_policy else 30,
            require_human_approval=(
                return_policy.require_human_approval if return_policy else True
            ),
        )
        if decision.needs_human_approval:
            return {
                "response": f"已完成自动资格检查：{decision.reason}",
                "mode": "workflow",
                "citations": policy_citation,
                "needs_human": True,
                "ticket_reason": f"退货/退款人工审核：{order.id}；{decision.reason}",
                "ticket_priority": "normal",
                "trace": [
                    "return:ownership_verified",
                    "return:policy_checked",
                    "return:approval_required",
                ],
            }
        return {
            "response": decision.reason,
            "mode": "workflow",
            "citations": policy_citation,
            "needs_human": False,
            "trace": ["return:ownership_verified", "return:policy_checked", "return:closed"],
        }

    async def knowledge_workflow(self, state: SupportState) -> dict:
        hits = self._search(state, state["sanitized_message"], limit=1)
        if not hits:
            return {
                "response": "知识库里没有找到可靠答案，我可以为你转接人工客服。",
                "mode": "workflow",
                "citations": [],
                "trace": ["knowledge:no_grounding"],
            }
        answer = "\n\n".join(hit.content for hit in hits)
        return {
            "response": answer,
            "mode": "workflow",
            "citations": [{"source_id": hit.source_id, "title": hit.title} for hit in hits],
            "trace": [f"knowledge:retrieved:{len(hits)}"],
        }

    async def dynamic_agent(self, state: SupportState) -> dict:
        if not self.settings.llm_available:
            hits = self._search(state, state["sanitized_message"], limit=2)
            if hits:
                response = "\n\n".join(hit.content for hit in hits)
                citations = [{"source_id": hit.source_id, "title": hit.title} for hit in hits]
            else:
                response = (
                    "我目前以离线模式运行。你可以询问订单状态、退货退款、配送、保修或商品信息；"
                    "也可以在 .env 中配置 OPENAI_API_KEY 启用动态工具调用。"
                )
                citations = []
            return {
                "response": response,
                "mode": "offline_fallback",
                "citations": citations,
                "trace": ["agent:offline_fallback"],
            }

        repo = state["repo"]
        customer_id = state["customer_id"]
        captured_citations: dict[str, str] = {}

        @tool
        def search_store_help(query: str) -> str:
            """Search store policies and the product catalog. Use this for factual answers."""
            hits = self._search(state, query, limit=3)
            for hit in hits:
                captured_citations[hit.source_id] = hit.title
            return json.dumps(_hits_payload(hits), ensure_ascii=False)

        @tool
        async def get_my_order(order_id: str) -> str:
            """Get one order belonging to the authenticated customer."""
            try:
                order = await repo.get_order(customer_id, order_id.upper())
            except NotFoundError:
                return json.dumps({"error": "order_not_found"})
            return json.dumps(_order_payload(order), ensure_ascii=False)

        @tool
        async def list_my_recent_orders() -> str:
            """List recent orders belonging to the authenticated customer."""
            orders = await repo.list_orders(customer_id)
            return json.dumps([_order_payload(order) for order in orders], ensure_ascii=False)

        tools = [search_store_help, get_my_order, list_my_recent_orders]
        tools_by_name = {item.name: item for item in tools}
        model = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
            temperature=0,
            timeout=20,
            max_retries=2,
        ).bind_tools(tools)
        pack = state.get("domain_pack")
        domain_prompt = ""
        if pack:
            domain_prompt = (
                f"\n当前领域：{pack.name}（{pack.vertical}）。默认语言：{pack.default_locale}。"
                f"语气：{pack.persona.tone}。\n"
                + "\n".join(f"- {rule}" for rule in pack.persona.response_rules)
            )
        messages: list = [SystemMessage(content=SYSTEM_PROMPT + domain_prompt)]
        for item in state.get("history", []):
            if item["role"] == "user":
                messages.append(HumanMessage(content=item["content"]))
            elif item["role"] == "assistant":
                messages.append(AIMessage(content=item["content"]))
        messages.append(HumanMessage(content=state["sanitized_message"]))
        tool_steps = 0
        input_tokens = 0
        output_tokens = 0
        try:
            pack = state.get("domain_pack")
            max_steps = pack.safety.max_agent_steps if pack else self.settings.max_agent_steps
            for _ in range(max_steps):
                ai_message = await model.ainvoke(messages)
                messages.append(ai_message)
                usage: dict[str, Any] = dict(ai_message.usage_metadata or {})
                input_tokens += int(usage.get("input_tokens", 0))
                output_tokens += int(usage.get("output_tokens", 0))
                if not ai_message.tool_calls:
                    content = ai_message.content
                    if isinstance(content, list):
                        content = "".join(
                            str(part.get("text", "")) if isinstance(part, dict) else str(part)
                            for part in content
                        )
                    return {
                        "response": str(content).strip() or "我暂时无法生成可靠回答。",
                        "mode": "agent",
                        "citations": [
                            {"source_id": source_id, "title": title}
                            for source_id, title in captured_citations.items()
                        ],
                        "token_usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                        },
                        "trace": [f"agent:tool_steps:{tool_steps}", "agent:completed"],
                    }
                for call in ai_message.tool_calls:
                    selected_tool = tools_by_name.get(call["name"])
                    if selected_tool is None:
                        result = json.dumps({"error": "tool_not_allowed"})
                    else:
                        result = await selected_tool.ainvoke(call["args"])
                        TOOL_CALLS.labels(tool=selected_tool.name).inc()
                    messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
                    tool_steps += 1
        except Exception:
            logger.exception("Dynamic agent failed", extra={"request_id": "agent"})
            return {
                "response": "模型服务暂时不可用，已为你创建人工处理工单。",
                "mode": "agent",
                "citations": [],
                "needs_human": True,
                "ticket_reason": "动态 Agent 调用失败，需要人工回复",
                "ticket_priority": "normal",
                "trace": ["agent:error", "agent:fallback_handoff"],
            }

        return {
            "response": "这个问题需要更多判断，已为你转接人工客服。",
            "mode": "agent",
            "citations": [],
            "needs_human": True,
            "ticket_reason": "动态 Agent 达到最大工具调用步数",
            "ticket_priority": "normal",
            "trace": ["agent:step_limit", "agent:fallback_handoff"],
        }

    async def human_handoff(self, state: SupportState) -> dict:
        reason = state.get("ticket_reason") or f"顾客要求人工处理：{state['sanitized_message']}"
        priority = state.get(
            "ticket_priority", "high" if state["intent"] == "human_handoff" else "normal"
        )
        pack = state.get("domain_pack")
        normalized = state.get("sanitized_message", "").lower()
        if pack and any(keyword.lower() in normalized for keyword in pack.handoff.urgent_keywords):
            priority = "urgent"
        sla_minutes = (
            pack.handoff.urgent_minutes
            if pack and priority in {"urgent", "high"}
            else pack.handoff.normal_minutes
            if pack
            else 240
        )
        ticket = await state["repo"].create_ticket(
            conversation_id=state["conversation_id"],
            customer_id=state["customer_id"],
            order_id=state.get("order_id"),
            reason=reason,
            priority=priority,
            channel=state.get("channel", "web"),
            sla_due_at=datetime.now(UTC) + timedelta(minutes=sla_minutes),
        )
        prefix = state.get("response", "已记录你的请求。")
        return {
            "response": (
                f"{prefix} 工单 {ticket.id} 已创建，人工客服会在约 {sla_minutes} 分钟内继续处理。"
            ),
            "ticket_id": ticket.id,
            "ticket_priority": priority,
            "mode": state.get("mode", "workflow"),
            "citations": state.get("citations", []),
            "needs_human": False,
            "trace": ["handoff:ticket_persisted"],
        }
