"""On-demand model verification, protected by the app's global auth middleware.

Mount ``router`` with ``app.include_router(router)``; do not exempt /admin from
application authentication. No credentials, endpoints, or deployment names are
accepted from requests. This uses the same cached provider as normal chat.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from harness.config import ROUTES, RouteName
from scripts.verify_models import RouteResult, verify_routes

router = APIRouter(prefix="/admin", tags=["verification"])
VERIFICATION_TIMEOUT = 90.0
REQUEST_TIMEOUT = 30.0
_verification_lock = asyncio.Lock()


class VerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routes: list[RouteName] | None = Field(default=None, min_length=1, max_length=5)


@router.post("/verify-models")
async def verify_models(request: VerificationRequest | None = None) -> dict:
    """Paid runtime-identity probe; returns 200 even when models are not ready.

    At most one verification per process. The sequential verifier enforces both
    per-request and total time budgets, with SDK retries disabled. Cancellation
    always releases the gate; /health remains independent and non-blocking.
    """
    names = list(dict.fromkeys(request.routes)) if request and request.routes else list(ROUTES)
    if not names or any(name not in ROUTES for name in names):
        raise HTTPException(422, "Choose one or more configured route names")
    # No await between the check and uncontended acquire: atomic on the app loop.
    if _verification_lock.locked():
        raise HTTPException(429, "Model verification already running", headers={"Retry-After": "5"})
    async with _verification_lock:
        results: list[RouteResult] = await verify_routes(
            names, timeout=REQUEST_TIMEOUT, total_timeout=VERIFICATION_TIMEOUT,
        )
    ready = bool(results) and all(
        r.reachable and not r.failed and (not r.expects_tools or r.tools_work is True)
        for r in results
    )
    return {"ready": ready, "results": [r.as_dict() for r in results]}
