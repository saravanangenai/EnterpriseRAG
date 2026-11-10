"""Ingestion pipeline: parse -> caption images -> chunk -> PII -> embed -> upsert -> record."""
import asyncio
import hashlib
import logging
from pathlib import Path

import aiosqlite

from app.db import bump_corpus_version
from app.ingestion.chunking import Chunk, chunk_blocks
from app.ingestion.parsers import Block, ParseContext, parse_file
from app.pii import PiiScanner
from app.prompts import PromptSet, load_prompts
from app.providers.base import Embedder, VisionCaptioner
from app.settings import AppConfig, Settings
from app.tracing import NullTracer, span
from app.vectorstore import Point, VectorStore

log = logging.getLogger("ingestion")

# The caption prompt is prompts/caption/v<N>.yaml; config.yaml picks the active version.


class Ingestor:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings,
        embedder: Embedder,
        vision: VisionCaptioner,
        store: VectorStore,
        sparse,
        pii: PiiScanner,
        tracer=None,
        prompts: PromptSet | None = None,
    ):
        self.tracer = tracer or NullTracer()
        self.caption_prompt = (prompts or load_prompts(cfg))["caption"]
        self.cfg, self.settings = cfg, settings
        self.embedder, self.vision, self.store = embedder, vision, store
        self.sparse, self.pii = sparse, pii
        self.data_dir = Path(settings.data_dir)
        self._one_at_a_time = asyncio.Semaphore(1)  # 1 GB VM: one document at a time

    # ------------------------------------------------------------ helpers
    async def _set_status(self, pk: int, status: str, error: str | None = None, count: int = 0):
        async with aiosqlite.connect(self.settings.db_path) as db:
            await db.execute(
                "UPDATE documents SET status=?, error=?, chunk_count=? WHERE id=?",
                (status, error, count, pk),
            )
            await db.commit()

    async def _caption(self, block: Block, sem: asyncio.Semaphore) -> bool:
        """Returns True when the caption came from the cache (no vision call)."""
        path = self.data_dir / block.asset_path
        data = path.read_bytes()
        cp = self.caption_prompt
        # Keyed by image AND prompt: a new caption prompt really produces new captions.
        key = f"{hashlib.sha256(data).hexdigest()}_{cp.sha256[:16]}"
        cache = self.data_dir / "captions" / f"{key}.txt"
        if cache.exists():  # caption each image once per prompt version
            block.text = cache.read_text(encoding="utf-8")
            return True
        async with sem:
            text = await self.vision.caption(data, block.mime or "image/png", cp.render())
        text = text.strip()
        if text:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        block.text = text
        return False

    async def _apply_pii(self, chunks: list[Chunk]) -> None:
        mode = self.cfg.pii.mode
        if mode == "off":
            return
        for c in chunks:
            flags = await asyncio.to_thread(self.pii.scan, c.text)
            c.pii_flags = flags
            if flags and mode == "mask":
                c.text = await asyncio.to_thread(self.pii.mask, c.text)

    # ------------------------------------------------------------ main entry
    async def run(self, pk: int) -> None:
        async with self._one_at_a_time:
            root = self.tracer.start(
                "ingest", user_id=None, tags=["ingest"], metadata={"document_row": pk}
            )
            try:
                await self._run(pk, root)
            except Exception as e:  # noqa: BLE001 - record any failure on the document
                log.exception("ingestion failed for document row %s", pk)
                root.update(level="ERROR", status_message=type(e).__name__)
                await self._set_status(pk, "failed", error=f"{type(e).__name__}: {e}"[:500])
                # Batches stored before the failure are searchable: invalidate cached answers.
                async with aiosqlite.connect(self.settings.db_path) as db:
                    await bump_corpus_version(db)
                    await db.commit()
            finally:
                root.end()

    async def _run(self, pk: int, root) -> None:
        async with aiosqlite.connect(self.settings.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM documents WHERE id=?", (pk,))).fetchone()
        if row is None:
            return
        document_id, version = row["document_id"], row["version"]
        # File names can identify people, so they are traced only when content logging is on.
        root.update(
            input=self.tracer.text(row["filename"]),
            metadata={
                "document_id": document_id, "version": version, "ext": Path(row["filename"]).suffix.lower(),
                "prompts": {"caption": self.caption_prompt.id},
            },
        )
        await self._set_status(pk, "processing")
        await self.store.ensure()

        ctx = ParseContext(
            data_dir=self.data_dir,
            assets_dir=self.data_dir / "assets" / document_id / f"v{version}",
            image_min_px=self.cfg.ingestion.image_min_px,
            pdf_render_dpi=self.cfg.ingestion.pdf_render_dpi,
        )
        with span(root, "parse") as s:
            blocks = await asyncio.to_thread(parse_file, self.data_dir / row["raw_path"], ctx)
            kinds = {k: sum(1 for b in blocks if b.kind == k) for k in ("text", "table", "image")}
            s.update(output=kinds)

        images = [b for b in blocks if b.kind == "image"]
        with span(root, "caption", as_type="generation", model=getattr(self.vision, "model", None),
                  input={"images": len(images)}, metadata={"prompt": self.caption_prompt.id}) as s:
            sem = asyncio.Semaphore(self.cfg.ingestion.caption_concurrency)
            cached = await asyncio.gather(*(self._caption(b, sem) for b in images))
            s.update(output={"captioned": len(images) - sum(cached), "from_cache": sum(cached)})

        with span(root, "chunk") as s:
            chunks = chunk_blocks(blocks, self.cfg.chunking.size, self.cfg.chunking.overlap_pct)
            s.update(output={"chunks": len(chunks)})
        with span(root, "pii", as_type="guardrail") as s:
            await self._apply_pii(chunks)
            s.update(output={"chunks_with_pii": sum(1 for c in chunks if c.pii_flags), "mode": self.cfg.pii.mode})
        if not chunks:
            raise ValueError("No extractable content found in the file")

        # De-duplicate identical chunks inside the document, keep order.
        by_id: dict[str, Chunk] = {}
        for c in chunks:
            by_id.setdefault(c.point_id(document_id), c)
        ids = list(by_id)

        existing = await self.store.existing_ids(ids)
        new_ids = [i for i in ids if i not in existing]

        def payload(c: Chunk) -> dict:
            return {
                "document_id": document_id,
                "version": version,
                "filename": row["filename"],
                "file_sha256": row["file_sha256"],
                "page": c.page,
                "type": c.type,
                "section": c.section,
                "chunk_sha256": c.sha256,
                "asset_path": c.asset_path,
                "access_group": None,  # placeholder, unused in v1
                "pii_flags": c.pii_flags or [],
                # Which caption prompt wrote this text (image chunks only), to find chunks to re-caption.
                "caption_prompt": self.caption_prompt.id if c.type == "image" else None,
                "text": c.text,
            }

        # Unchanged chunks: no embedding call, just refresh metadata.
        for i in ids:
            if i in existing:
                await self.store.update_payload(i, payload(by_id[i]))

        batch = self.cfg.ingestion.embed_batch
        with span(root, "embed_and_upsert", as_type="embedding", model=getattr(self.embedder, "model", None),
                  input={"new": len(new_ids), "unchanged": len(existing)}) as s:
            for st in range(0, len(new_ids), batch):
                part = new_ids[st : st + batch]
                texts = [by_id[i].text for i in part]
                dense = await self.embedder.embed(texts)
                sparse = await asyncio.to_thread(self.sparse.embed, texts)
                await self.store.upsert(
                    [
                        Point(i, d, sp[0], sp[1], payload(by_id[i]))
                        for i, d, sp in zip(part, dense, sparse, strict=True)
                    ]
                )
            s.update(output={"embedded": len(new_ids), "batches": -(-len(new_ids) // batch)})

        # Remove chunks that belonged to older versions and no longer exist.
        stale = await self.store.doc_point_ids(document_id) - set(ids)
        if stale:
            await self.store.delete(sorted(stale))
        root.update(output={"chunks": len(ids), "new": len(new_ids), "unchanged": len(existing), "removed": len(stale)})

        async with aiosqlite.connect(self.settings.db_path) as db:
            await db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            await db.executemany(
                "INSERT INTO chunks (chunk_id, document_id, version, page, type, chunk_sha256,"
                " asset_path, access_group, pii_flags) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        i, document_id, version, c.page, c.type, c.sha256, c.asset_path, None,
                        ",".join(c.pii_flags or []),
                    )
                    for i, c in by_id.items()
                ],
            )
            await db.execute(
                "UPDATE documents SET status='superseded' WHERE document_id=? AND version<? "
                "AND status='ready'",
                (document_id, version),
            )
            await bump_corpus_version(db)  # cached answers may cite chunks that just changed
            await db.commit()
        await self._set_status(pk, "ready", count=len(ids))
        log.info(
            "ingested %s v%s: %d chunks (%d new, %d unchanged, %d removed)",
            row["filename"], version, len(ids), len(new_ids), len(existing), len(stale),
        )
