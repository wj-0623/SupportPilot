"""Configuration-driven intent routing with deterministic fallbacks."""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.config import DomainPackConfig
from app.domain.intents import IntentResult, classify_intents


@dataclass(frozen=True)
class RoutingDecision:
    primary: IntentResult
    intents: tuple[IntentResult, ...]
    route: str
    reason: str


def decide_route(text: str, pack: DomainPackConfig) -> RoutingDecision:
    """Apply Domain Pack order, thresholds and route declarations to one turn."""

    configured = {item.name: item for item in pack.intents}
    order = {name: index for index, name in enumerate(pack.workflow_order)}
    candidates = classify_intents(
        text,
        pack.intent_patterns(),
        limit=pack.safety.max_intents_per_turn,
    )
    qualified = [
        item
        for item in candidates
        if item.name in configured and item.confidence >= configured[item.name].confidence_threshold
    ]
    qualified.sort(key=lambda item: (order.get(item.name, len(order)), -item.confidence))
    if qualified:
        primary = qualified[0]
        return RoutingDecision(
            primary=primary,
            intents=tuple(qualified),
            route=configured[primary.name].route,
            reason="configured_intent",
        )

    fallback = configured.get("general")
    if fallback is None:
        primary = IntentResult("general", 0.0)
        return RoutingDecision(primary, (primary,), "handoff", "no_general_fallback")
    primary = IntentResult("general", candidates[0].confidence if candidates else 0.0)
    return RoutingDecision(primary, (primary,), fallback.route, "below_threshold")
