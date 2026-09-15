from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.domain.config import DomainPackConfig


class DomainPackRegistry:
    """Loads validated built-in packs; database revisions are managed by the admin API."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._packs: dict[str, DomainPackConfig] = {}
        self.reload()

    def reload(self) -> None:
        packs: dict[str, DomainPackConfig] = {}
        for path in sorted(self.directory.glob("*.json")):
            pack = DomainPackConfig.model_validate_json(path.read_text(encoding="utf-8"))
            if pack.slug in packs:
                raise ValueError(f"duplicate domain pack slug: {pack.slug}")
            packs[pack.slug] = pack
        if "general-commerce" not in packs:
            raise ValueError("general-commerce domain pack is required")
        self._packs = packs

    def get(self, slug: str) -> DomainPackConfig:
        try:
            return self._packs[slug]
        except KeyError as exc:
            raise KeyError(f"unknown domain pack: {slug}") from exc

    def list(self) -> list[DomainPackConfig]:
        return list(self._packs.values())

    def export(self, slug: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.get(slug).model_dump_json()))
