from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from app.db.models import KnowledgeSource


@dataclass(frozen=True)
class KnowledgeHit:
    source_id: str
    title: str
    content: str
    score: float


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9-]+", lowered)
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", lowered)
    chinese_tokens: list[str] = []
    for chunk in chinese_chunks:
        chinese_tokens.extend(list(chunk))
        chinese_tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return latin + chinese_tokens


def hashed_vector(tokens: Counter[str], dimensions: int = 128) -> list[float]:
    vector = [0.0] * dimensions
    for token, frequency in tokens.items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * (1 + math.log(frequency))
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


class KnowledgeBase:
    def __init__(
        self, knowledge_path: Path, catalog_path: Path, *, include_bundled: bool = True
    ) -> None:
        self.documents = (
            self._load(knowledge_path) + self._load(catalog_path) if include_bundled else []
        )
        self.document_tokens = [Counter(tokenize(self._searchable(doc))) for doc in self.documents]
        self.document_frequency = Counter(
            token for tokens in self.document_tokens for token in tokens
        )

    @staticmethod
    def _load(path: Path) -> list[dict[str, str]]:
        return cast(list[dict[str, str]], json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def _searchable(document: dict[str, str]) -> str:
        return " ".join(str(value) for value in document.values())

    def search(self, query: str, limit: int = 3) -> list[KnowledgeHit]:
        return self._search_documents(
            query, self.documents, self.document_tokens, self.document_frequency, limit
        )

    def search_with_tenant_documents(
        self, query: str, tenant_documents: list[dict[str, str]], limit: int = 3
    ) -> list[KnowledgeHit]:
        if not tenant_documents:
            return self.search(query, limit)
        documents = self.documents + tenant_documents
        tokens = [Counter(tokenize(self._searchable(doc))) for doc in documents]
        frequency = Counter(token for item in tokens for token in item)
        return self._search_documents(query, documents, tokens, frequency, limit)

    @staticmethod
    def _search_documents(
        query: str,
        documents: list[dict[str, str]],
        document_tokens: list[Counter[str]],
        document_frequency: Counter[str],
        limit: int,
    ) -> list[KnowledgeHit]:
        query_tokens = Counter(tokenize(query))
        if not query_tokens:
            return []
        total_documents = max(len(documents), 1)
        lexical_scores: list[tuple[dict[str, str], Counter[str], float]] = []
        for document, tokens in zip(documents, document_tokens, strict=True):
            score = 0.0
            for token, query_frequency in query_tokens.items():
                term_frequency = tokens.get(token, 0)
                if not term_frequency:
                    continue
                inverse_frequency = math.log(1 + total_documents / (1 + document_frequency[token]))
                score += query_frequency * (1 + math.log(term_frequency)) * inverse_frequency
            lexical_scores.append((document, tokens, score))
        maximum_lexical = max((item[2] for item in lexical_scores), default=0.0) or 1.0
        query_vector = hashed_vector(query_tokens)
        scored: list[KnowledgeHit] = []
        for document, tokens, lexical in lexical_scores:
            semantic = max(0.0, cosine(query_vector, hashed_vector(tokens)))
            title_overlap = bool(set(query_tokens) & set(tokenize(document["title"])))
            hybrid = 0.65 * (lexical / maximum_lexical) + 0.3 * semantic
            if title_overlap:
                hybrid += 0.05
            if hybrid > 0:
                scored.append(
                    KnowledgeHit(
                        source_id=document["id"],
                        title=document["title"],
                        content=document["content"],
                        score=round(min(hybrid, 1.0), 4),
                    )
                )
        return sorted(scored, key=lambda hit: hit.score, reverse=True)[:limit]


FRESHNESS_SENSITIVE_SOURCE_TYPES = {"url", "sitemap", "help-center", "catalog", "api"}


def partition_fresh_knowledge(
    sources: list[KnowledgeSource], freshness_hours: int, *, now: datetime | None = None
) -> tuple[list[KnowledgeSource], list[KnowledgeSource]]:
    """Separate usable knowledge from external sources that exceeded their freshness policy."""

    cutoff = (now or datetime.now(UTC)) - timedelta(hours=freshness_hours)
    fresh: list[KnowledgeSource] = []
    stale: list[KnowledgeSource] = []
    for source in sources:
        metadata = json.loads(source.metadata_json)
        expires = source.source_type in FRESHNESS_SENSITIVE_SOURCE_TYPES and not metadata.get(
            "freshness_exempt", False
        )
        synced_at = (
            source.synced_at.replace(tzinfo=UTC)
            if source.synced_at.tzinfo is None
            else source.synced_at
        )
        (stale if expires and synced_at < cutoff else fresh).append(source)
    return fresh, stale
