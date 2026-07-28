"""Workload-identity endpoints: OIDC discovery, JWKS, and the
per-effect ID token endpoint (see workload_identity.py)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from nixbot.effects_state import TaskClaim, TaskTokens
    from nixbot.workload_identity import IdentityIssuer

logger = logging.getLogger(__name__)

# Cap per effect run: an effect has no legitimate reason to mint more,
# even when re-requesting throughout a long deploy.
MAX_ID_TOKENS_PER_RUN = 100


class IdTokenRequest(BaseModel):
    audience: str


def create_workload_identity_router(
    issuer: IdentityIssuer, tokens: TaskTokens
) -> APIRouter:
    router = APIRouter()

    @router.get("/.well-known/openid-configuration", include_in_schema=False)
    async def discovery() -> dict:
        return issuer.discovery_document()

    @router.get("/.well-known/jwks.json", include_in_schema=False)
    async def jwks() -> dict:
        return issuer.jwks()

    def _claim(request: Request) -> TaskClaim:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        claim = tokens.claim_for(token) if scheme.lower() == "bearer" else None
        if claim is None:
            raise HTTPException(status_code=401, detail="invalid task token")
        return claim

    @router.post("/api/v1/id-token")
    async def id_token(request: Request, body: IdTokenRequest) -> dict:
        claim = _claim(request)
        identity = claim.identity
        if identity is None:
            raise HTTPException(
                status_code=403, detail="this run cannot request identity tokens"
            )
        if body.audience not in identity.allowed_audiences:
            raise HTTPException(
                status_code=403,
                detail="audience not declared in the effect's idTokenAudiences",
            )
        if claim.id_tokens_issued >= MAX_ID_TOKENS_PER_RUN:
            raise HTTPException(status_code=429, detail="id token limit reached")
        claim.id_tokens_issued += 1
        issued = issuer.mint(identity, body.audience)
        logger.info(
            "issued id token",
            extra={
                "build_id": identity.build_id,
                "effect": identity.effect,
                "repository": identity.repository,
                "audience": body.audience,
            },
        )
        return {"token": issued.token, "expires_at": issued.expires_at.isoformat()}

    return router
