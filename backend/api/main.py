"""FastAPI transport: SSE streaming, warmup, thread CRUD.

The API layer is deliberately thin -- it translates harness events to SSE and
nothing else.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from harness.config import settings
from harness.context import Conversation
from harness.loop import Harness, approvals, generate_title
from harness.persistence import new_thread_id, store
from harness.router import route_table
from harness.tools import builtin  # noqa: F401 - registers tools on import
from harness.tools.registry import registry

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "harness up: %d tools, routes=%s",
        len(registry),
        [r["route"] for r in route_table()],
    )
    yield


app = FastAPI(title="agent-harness", version="0.1.0", lifespan=lifespan)

# Last registered is outermost: CORS validates preflights and decorates 401/503.
from api.auth import OwnerAuthMiddleware

app.add_middleware(OwnerAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None
    route: str | None = Field(default=None, description="Force a route tier")
    enable_tools: bool = True


class ApprovalRequest(BaseModel):
    call_id: str
    approved: bool


# ---------------------------------------------------------------------------
# Health / warmup
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/warmup")
async def warmup() -> dict:
    """Called by the frontend on page load.

    The backend runs with min-replicas 0 to cost ~$0 at idle, which means a
    3-10s cold start. Firing this while the user is still typing hides it.
    """
    return {"status": "warm", "tools": len(registry)}


@app.get("/routes")
async def routes() -> dict:
    return {"routes": route_table(), "default": "chat"}


@app.get("/tools")
async def tools() -> dict:
    return {
        "tools": [
            {"name": t.name, "description": t.description, "dangerous": t.dangerous}
            for t in registry._tools.values()  # noqa: SLF001
        ]
    }


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------
@app.get("/threads")
async def list_threads() -> dict:
    return {"threads": await store.list_threads()}


@app.get("/threads/{thread_id}")
async def get_thread(thread_id: str) -> dict:
    data = await store.load(thread_id)
    if data is None:
        raise HTTPException(404, "thread not found")
    return data


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str) -> dict:
    await store.delete(thread_id)
    return {"deleted": thread_id}


@app.post("/approve")
async def approve(req: ApprovalRequest) -> dict:
    ok = approvals.resolve(req.call_id, req.approved)
    if not ok:
        raise HTTPException(404, "no pending approval for that call_id")
    return {"resolved": req.call_id, "approved": req.approved}


# ---------------------------------------------------------------------------
# Chat (SSE)
# ---------------------------------------------------------------------------
def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    thread_id = req.thread_id or new_thread_id()
    saved = await store.load(thread_id) if req.thread_id else None

    conv = Conversation()
    title = ""
    if saved:
        conv.messages = saved.get("messages", [])
        conv.compactions = saved.get("compactions", 0)
        title = saved.get("title", "")

    harness = Harness(conv)

    async def event_stream():
        yield _sse({"type": "thread", "thread_id": thread_id})
        try:
            async for event in harness.run(
                req.message, forced_route=req.route, enable_tools=req.enable_tools
            ):
                yield _sse(event)
        except asyncio.CancelledError:
            log.info("client disconnected from thread %s", thread_id)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("stream failed")
            yield _sse({"type": "error", "message": str(exc)})

        nonlocal title
        if not title:
            title = await generate_title(req.message)
            yield _sse({"type": "title", "title": title})

        await store.save(
            thread_id,
            {
                "id": thread_id,
                "title": title,
                "messages": conv.messages,
                "compactions": conv.compactions,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
