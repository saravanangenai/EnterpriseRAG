import json
import logging
from pathlib import Path
from typing import Literal

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.auth import get_current_user
from app.db import get_db
from app.deps import get_rag
from app.guardrails import GuardrailBlocked
from app.retrieval.service import RagService
from app.settings import get_settings
from app.signing import verify

router = APIRouter(tags=["chat"])
log = logging.getLogger("uvicorn.error")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    document_id: str | None = None
    type: Literal["text", "table", "image"] | None = None


async def _admit(req: ChatRequest, user, db: aiosqlite.Connection, svc: RagService) -> None:
    try:
        svc.guard.check_input(req.question)
    except GuardrailBlocked as e:
        raise HTTPException(400, e.message)
    cur = await db.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE user=? AND date(timestamp)=date('now')",
        (user["username"],),
    )
    if (await cur.fetchone())["n"] >= svc.guard.policy.daily_request_limit:
        raise HTTPException(429, "Daily request limit reached")


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: aiosqlite.Row = Depends(get_current_user),
    svc: RagService = Depends(get_rag),
):
    await _admit(req, user, db, svc)
    final = None
    try:
        async for ev in svc.stream(user["username"], req.question, req.document_id, req.type):
            if ev["type"] == "final":
                final = ev
    except GuardrailBlocked as e:
        raise HTTPException(400, e.message)
    return {k: v for k, v in final.items() if k != "type"}


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    db: aiosqlite.Connection = Depends(get_db),
    user: aiosqlite.Row = Depends(get_current_user),
    svc: RagService = Depends(get_rag),
):
    await _admit(req, user, db, svc)

    async def events():
        try:
            async for ev in svc.stream(user["username"], req.question, req.document_id, req.type):
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
        except Exception as e:  # tell the client instead of dropping the connection
            log.exception("chat stream failed")
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'message': type(e).__name__})}\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
    )


class Feedback(BaseModel):
    trace_id: str = Field(min_length=8, max_length=64)
    value: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=500)


@router.post("/chat/feedback", status_code=204)
async def feedback(
    body: Feedback,
    db: aiosqlite.Connection = Depends(get_db),
    user: aiosqlite.Row = Depends(get_current_user),
    svc: RagService = Depends(get_rag),
):
    """Thumbs up/down on an answer. Only the user who asked can rate that answer."""
    cur = await db.execute(
        "SELECT 1 FROM audit_log WHERE trace_id=? AND user=? LIMIT 1", (body.trace_id, user["username"])
    )
    if not await cur.fetchone():
        raise HTTPException(404, "No answer with that trace ID for this user")
    comment = body.comment
    if comment and svc.cfg.pii.mode != "off":
        comment = svc.pii.mask(comment)  # free text can hold personal data
    svc.tracer.score(body.trace_id, "user_feedback", 1.0 if body.value == "up" else 0.0, comment)


@router.get("/assets/{rest:path}")
async def get_asset(rest: str, exp: int, sig: str):
    """Serve a stored image only with a valid, unexpired signature."""
    rel = f"assets/{rest}"
    if not verify(rel, exp, sig):
        raise HTTPException(403, "Invalid or expired link")
    base = (Path(get_settings().data_dir) / "assets").resolve()
    target = (Path(get_settings().data_dir) / rel).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)
