from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentResult:
    name: str
    confidence: float


INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
    "human_handoff": ("人工", "客服人员", "投诉", "律师", "起诉", "chargeback", "human"),
    "return_refund": ("退款", "退货", "换货", "不想要", "refund", "return", "exchange"),
    "order_status": ("订单", "物流", "到哪", "快递", "tracking", "where is my order"),
    "product": (
        "商品",
        "产品",
        "有货",
        "库存",
        "参数",
        "兼容",
        "耳机",
        "键盘",
        "充电座",
        "windows",
        "macos",
        "product",
        "stock",
    ),
    "faq": ("运费", "包邮", "配送", "多久", "支付方式", "保修", "shipping", "warranty"),
}


def classify_intent(text: str, patterns: dict[str, tuple[str, ...]] | None = None) -> IntentResult:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    active_patterns = patterns or INTENT_PATTERNS
    scores = {
        intent: sum(1 for keyword in keywords if keyword in normalized)
        for intent, keywords in active_patterns.items()
    }
    if not scores:
        return IntentResult("general", 0.35)
    best_intent, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score == 0:
        return IntentResult("general", 0.35)
    confidence = min(0.99, 0.72 + 0.12 * (best_score - 1))
    return IntentResult(best_intent, confidence)


def classify_intents(
    text: str,
    patterns: dict[str, tuple[str, ...]] | None = None,
    *,
    limit: int = 3,
) -> list[IntentResult]:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    active_patterns = patterns or INTENT_PATTERNS
    ranked = sorted(
        (
            (intent, sum(1 for keyword in keywords if keyword in normalized))
            for intent, keywords in active_patterns.items()
            if intent != "general"
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    results = [
        IntentResult(intent, min(0.99, 0.72 + 0.12 * (score - 1)))
        for intent, score in ranked
        if score > 0
    ][:limit]
    return results or [IntentResult("general", 0.35)]


def extract_order_id(text: str) -> str | None:
    match = re.search(r"\bORD[-\s]?\d{4,12}\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(0).upper().replace(" ", "-")
