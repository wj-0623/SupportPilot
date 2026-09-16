from __future__ import annotations

import os
import re
from typing import Protocol

_ENV_REF = re.compile(r"^env://([A-Z][A-Z0-9_]{1,127})$")


class CredentialResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class EnvironmentCredentialResolver:
    """Resolve orchestrator-injected secrets without persisting secret values in the database."""

    def resolve(self, reference: str) -> str:
        match = _ENV_REF.fullmatch(reference)
        if not match:
            raise ValueError("credential_ref must use env://NAME")
        value = os.getenv(match.group(1))
        if not value:
            raise RuntimeError(f"credential is unavailable: {reference}")
        return value
