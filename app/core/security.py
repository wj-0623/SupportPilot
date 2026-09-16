from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, cast

import jwt
from fastapi import Header, HTTPException, Request, status
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import TenantMember


def secure_equals(candidate: str | None, expected: str | None) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


Role = Literal["customer", "agent", "admin", "service"]


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    role: Role
    customer_id: str | None = None

    def require_roles(self, *roles: Role) -> None:
        if self.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")


def _decode_segment(segment: str) -> dict[str, object]:
    padding = "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode((segment + padding).encode("ascii"))
        value = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=401, detail="Malformed bearer token") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=401, detail="Malformed bearer token")
    return value


def decode_hs256_token(token: str, settings: Settings) -> Principal:
    """Validate an OIDC-compatible HS256 access token without trusting client identity fields."""

    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="JWT authentication is not configured")
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="Malformed bearer token")
    header = _decode_segment(parts[0])
    claims = _decode_segment(parts[1])
    if header.get("alg") != "HS256" or header.get("typ") not in {None, "JWT"}:
        raise HTTPException(status_code=401, detail="Unsupported bearer token")
    expected = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{parts[0]}.{parts[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    padding = "=" * (-len(parts[2]) % 4)
    try:
        signature = base64.urlsafe_b64decode((parts[2] + padding).encode("ascii"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Malformed bearer token") from exc
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")

    now = int(time.time())
    skew = settings.jwt_clock_skew_seconds
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp < now - skew:
        raise HTTPException(status_code=401, detail="Bearer token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and nbf > now + skew:
        raise HTTPException(status_code=401, detail="Bearer token not active")
    if claims.get("iss") != settings.jwt_issuer:
        raise HTTPException(status_code=401, detail="Invalid token issuer")
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.jwt_audience not in audiences:
        raise HTTPException(status_code=401, detail="Invalid token audience")
    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    role = claims.get("role")
    if not isinstance(subject, str) or not isinstance(tenant_id, str):
        raise HTTPException(status_code=401, detail="Missing identity claims")
    if role not in {"customer", "agent", "admin", "service"}:
        raise HTTPException(status_code=401, detail="Invalid role claim")
    customer_id = claims.get("customer_id")
    if role == "customer" and not isinstance(customer_id, str):
        raise HTTPException(status_code=401, detail="Missing customer identity")
    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        role=cast(Role, role),
        customer_id=customer_id if isinstance(customer_id, str) else None,
    )


async def decode_access_token(token: str, settings: Settings) -> Principal:
    if not settings.jwt_jwks_url:
        return decode_hs256_token(token, settings)
    try:
        jwks_client = _jwks_client(settings.jwt_jwks_url)
        signing_key = await asyncio.to_thread(jwks_client.get_signing_key_from_jwt, token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_clock_skew_seconds,
            options={"require": ["exp", "iss", "aud", "sub", "tenant_id", "role"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc
    subject = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    role = claims.get("role")
    customer_id = claims.get("customer_id")
    if not isinstance(subject, str) or not isinstance(tenant_id, str):
        raise HTTPException(status_code=401, detail="Missing identity claims")
    if role not in {"customer", "agent", "admin", "service"}:
        raise HTTPException(status_code=401, detail="Invalid role claim")
    if role == "customer" and not isinstance(customer_id, str):
        raise HTTPException(status_code=401, detail="Missing customer identity")
    return Principal(
        subject=subject,
        tenant_id=tenant_id,
        role=cast(Role, role),
        customer_id=customer_id if isinstance(customer_id, str) else None,
    )


@lru_cache(maxsize=16)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def principal_dependency(settings: Settings, *roles: Role) -> Callable[..., Awaitable[Principal]]:
    async def verify(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
        x_admin_key: str | None = Header(default=None),
    ) -> Principal:
        if settings.auth_mode == "jwt":
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Bearer token required")
            principal = await decode_access_token(authorization[7:], settings)
        else:
            is_admin = secure_equals(x_admin_key, settings.admin_api_key or settings.app_api_key)
            if (settings.admin_api_key or settings.app_api_key) and not (
                is_admin or secure_equals(x_api_key, settings.app_api_key)
            ):
                raise HTTPException(status_code=401, detail="Invalid API key")
            role: Role = (
                "admin" if is_admin or any(r in {"agent", "admin"} for r in roles) else "customer"
            )
            principal = Principal(
                subject="development-user",
                tenant_id=settings.default_tenant_id,
                role=role,
                customer_id=None,
            )
        if roles:
            principal.require_roles(*roles)
        if settings.enforce_tenant_membership and principal.role in {"agent", "admin", "service"}:
            database = getattr(request.app.state, "database", None)
            if database is None:
                raise HTTPException(status_code=503, detail="Authorization store is unavailable")
            async with database.sessions() as session:
                member = await session.scalar(
                    select(TenantMember).where(
                        TenantMember.tenant_id == principal.tenant_id,
                        TenantMember.subject == principal.subject,
                        TenantMember.active.is_(True),
                    )
                )
            if member is None or member.role != principal.role:
                raise HTTPException(status_code=403, detail="Tenant membership is not active")
        request.state.principal = principal
        return principal

    return verify


def api_key_dependency(settings: Settings) -> Callable[..., Awaitable[None]]:
    async def verify(x_api_key: str | None = Header(default=None)) -> None:
        if settings.app_api_key and not secure_equals(x_api_key, settings.app_api_key):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return verify


def admin_key_dependency(settings: Settings) -> Callable[..., Awaitable[None]]:
    async def verify(x_admin_key: str | None = Header(default=None)) -> None:
        expected = settings.admin_api_key or settings.app_api_key
        if expected and not secure_equals(x_admin_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key"
            )

    return verify


def request_fingerprint(request: Request, body: bytes) -> str:
    payload = b"|".join([request.method.encode(), request.url.path.encode(), body])
    return hashlib.sha256(payload).hexdigest()


def verify_webhook_signature(
    body: bytes, signature: str, secret: str, timestamp: str | None = None
) -> bool:
    signed = (timestamp.encode("ascii") + b"." + body) if timestamp else body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)
