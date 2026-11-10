import hashlib
import re
import shutil
import uuid
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, UploadFile

from app.auth import get_current_user, require_roles
from app.db import get_db
from app.deps import get_ingestor, get_store
from app.ingestion.pipeline import Ingestor
from app.settings import get_config, get_settings

router = APIRouter(prefix="/documents", tags=["documents"])


def _safe_name(name: str) -> str:
    base = Path(name).name
    return re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:120] or "file"


def _document_id(filename: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "doc:" + filename.lower()))


@router.post("", status_code=202)
async def upload(
    file: UploadFile,
    background: BackgroundTasks,
    response: Response,
    db: aiosqlite.Connection = Depends(get_db),
    user: aiosqlite.Row = Depends(require_roles("admin", "ingestor")),
    ingestor: Ingestor = Depends(get_ingestor),
):
    cfg, settings = get_config(), get_settings()
    filename = _safe_name(file.filename or "")
    ext = Path(filename).suffix.lower()
    if ext not in cfg.ingestion.allowed_ext:
        raise HTTPException(415, f"File type {ext or '(none)'} is not supported")

    limit = cfg.ingestion.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File larger than {cfg.ingestion.max_upload_mb} MB")
    if not data:
        raise HTTPException(422, "Empty file")

    sha = hashlib.sha256(data).hexdigest()
    document_id = _document_id(filename)

    # Same bytes already ingested: skip (saves embedding and caption calls).
    cur = await db.execute(
        "SELECT document_id, version FROM documents WHERE file_sha256=? AND status IN ('ready','processing','pending')",
        (sha,),
    )
    dup = await cur.fetchone()
    if dup:
        response.status_code = 200
        return {"status": "duplicate", "document_id": dup["document_id"], "version": dup["version"]}

    cur = await db.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM documents WHERE document_id=?", (document_id,)
    )
    version = (await cur.fetchone())["v"] + 1

    rel = Path("raw") / document_id / f"v{version}_{filename}"
    dest = Path(settings.data_dir) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    cur = await db.execute(
        "INSERT INTO documents (document_id, version, uploaded_by, filename, file_sha256, raw_path)"
        " VALUES (?,?,?,?,?,?)",
        (document_id, version, user["user_id"], filename, sha, rel.as_posix()),
    )
    await db.commit()
    background.add_task(ingestor.run, cur.lastrowid)
    return {"status": "pending", "document_id": document_id, "version": version}


@router.get("")
async def list_documents(
    db: aiosqlite.Connection = Depends(get_db), _u: aiosqlite.Row = Depends(get_current_user)
):
    cur = await db.execute(
        "SELECT document_id, filename, version, status, chunk_count, error, created_at "
        "FROM documents d WHERE version = (SELECT MAX(version) FROM documents WHERE document_id=d.document_id) "
        "ORDER BY created_at DESC"
    )
    return [dict(r) for r in await cur.fetchall()]


@router.get("/{document_id}")
async def get_document(
    document_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _u: aiosqlite.Row = Depends(get_current_user),
):
    cur = await db.execute(
        "SELECT version, status, chunk_count, error, file_sha256, created_at FROM documents "
        "WHERE document_id=? ORDER BY version DESC",
        (document_id,),
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        raise HTTPException(404, "Document not found")
    return {"document_id": document_id, "versions": rows}


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: str,
    db: aiosqlite.Connection = Depends(get_db),
    _admin: aiosqlite.Row = Depends(require_roles("admin")),
    store=Depends(get_store),
):
    cur = await db.execute("SELECT 1 FROM documents WHERE document_id=? LIMIT 1", (document_id,))
    if not await cur.fetchone():
        raise HTTPException(404, "Document not found")
    await store.delete_document(document_id)
    await db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
    await db.execute("DELETE FROM documents WHERE document_id=?", (document_id,))
    await db.commit()
    data = Path(get_settings().data_dir)
    for sub in ("raw", "assets"):
        shutil.rmtree(data / sub / document_id, ignore_errors=True)
