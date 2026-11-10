"""Re-run ingestion for documents already in SQLite, from their raw files in data/raw.

Use it to:
- rebuild Qdrant after the collection was lost or recreated (`--all`): every chunk is embedded again;
- retry failed or interrupted ingestions (`--failed`);
- apply a new caption prompt or chunking change to existing documents (`--all`): chunk IDs are
  content-based, so only chunks whose text changed are embedded again, and images are captioned
  again only when the caption prompt changed.

Reindexing re-runs an existing version row; it never creates a new version.
"""
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass
class Target:
    pk: int
    document_id: str
    version: int
    filename: str
    status: str
    raw_path: str


# Per document: the newest 'ready' version if there is one (what search should serve), otherwise
# the newest version of any status.
_ALL = """
SELECT d.id, d.document_id, d.version, d.filename, d.status, d.raw_path
FROM documents d
WHERE d.id = COALESCE(
    (SELECT id FROM documents WHERE document_id = d.document_id AND status = 'ready'
     ORDER BY version DESC LIMIT 1),
    (SELECT id FROM documents WHERE document_id = d.document_id ORDER BY version DESC LIMIT 1))
"""

# Per document: the newest version, only when it failed. An older failed version that was later
# replaced is not retried.
_FAILED = """
SELECT d.id, d.document_id, d.version, d.filename, d.status, d.raw_path
FROM documents d
WHERE d.status = 'failed'
  AND d.version = (SELECT MAX(version) FROM documents WHERE document_id = d.document_id)
"""


async def select_targets(db_path, mode: str, document_id: str | None = None) -> list[Target]:
    if mode == "all":
        sql, args = _ALL, ()
    elif mode == "failed":
        sql, args = _FAILED, ()
    elif mode == "document":
        sql, args = _ALL + " AND d.document_id = ?", (document_id,)
    else:
        raise ValueError(f"unknown mode {mode!r}")
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(sql + " ORDER BY d.id", args)).fetchall()
    return [Target(*r) for r in rows]


async def _status(db_path, pk: int) -> tuple[str, str | None, int]:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT status, error, chunk_count FROM documents WHERE id=?", (pk,))).fetchone()
    return row[0], row[1], row[2]


async def reindex(ingestor, db_path, data_dir, targets: list[Target], dry_run: bool = False) -> list[dict]:
    """Ingest each target in turn. Never raises for one bad document; returns one result per target."""
    results = []
    for t in targets:
        res = {"document_id": t.document_id, "version": t.version, "filename": t.filename, "before": t.status}
        if not (Path(data_dir) / t.raw_path).is_file():
            results.append({**res, "result": "skipped", "error": f"raw file missing: {t.raw_path}"})
            continue
        if dry_run:
            results.append({**res, "result": "would_reindex"})
            continue
        await ingestor.run(t.pk)  # records any failure on the row itself
        status, error, chunks = await _status(db_path, t.pk)
        results.append({**res, "result": status, "chunks": chunks, "error": error})
    return results
