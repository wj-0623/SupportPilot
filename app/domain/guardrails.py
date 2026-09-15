from __future__ import annotations

import re
from dataclasses import dataclass

INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?previous",
    r"reveal\s+(the\s+)?(system|developer)\s+prompt",
    r"system\s+prompt",
    r"忽略.{0,8}(之前|以上).{0,8}(指令|提示)",
    r"(泄露|显示|输出).{0,8}(系统提示|开发者指令)",
)

SENSITIVE_PATTERNS = (
    r"\b\d{17}[0-9Xx]\b",
    r"\b(?:\d[ -]*?){13,19}\b",
)


@dataclass(frozen=True)
class SafetyResult:
    safe_for_agent: bool
    sanitized_text: str
    reasons: tuple[str, ...]


def inspect_message(text: str) -> SafetyResult:
    reasons: list[str] = []
    if any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in INJECTION_PATTERNS):
        reasons.append("prompt_injection")

    sanitized = text
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, sanitized):
            reasons.append("sensitive_number_redacted")
            sanitized = re.sub(pattern, "[敏感号码已隐藏]", sanitized)

    return SafetyResult(
        safe_for_agent="prompt_injection" not in reasons,
        sanitized_text=sanitized,
        reasons=tuple(dict.fromkeys(reasons)),
    )
