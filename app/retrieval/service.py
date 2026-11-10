"""Query pipeline: guard -> embed -> hybrid search -> gate -> rerank -> prompt -> answer -> check.

Every stage is a Langfuse span (see app/tracing.py). Retrieval spans log IDs, scores and counts,
never document text, so traces stay free of personal data unless `tracing.log_content` is on.
"""
import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite

from app.db import corpus_version
from app.guardrails import Guard, GuardrailBlocked
from app.ingestion.sparse import Bm25Embedder
from app.pii import PiiScanner
from app.prompts import PromptSet, load_prompts
from app.providers.base import LLM, Embedder
from app.retrieval.answer_cache import AnswerCache, CachedAnswer, cache_key, normalize_question
from app.retrieval.rerank import rerank
from app.settings import AppConfig, Settings
from app.signing import signed_url
from app.tracing import NullTracer, error_fields, span
from app.vectorstore import Hit, VectorStore

# Prompt texts live in prompts/<name>/v<N>.yaml; config.yaml picks the active versions.


def _tag(h: Hit, i: int) -> str:
    p = h.payload
    bits = [p.get("filename", "?")]
    if p.get("page"):
        bits.append(f"p.{p['page']}")
    if p.get("section"):
        bits.append(f"section: {p['section']}")
    bits.append(f"type: {p.get('type', 'text')}")
    return f"[S{i}] ({', '.join(bits)})"


def _brief(hits: list[Hit]) -> list[dict]:
    """What a trace may record about a retrieved chunk: no text."""
    return [
        {
            "id": h.id,
            "score": round(h.score, 4),
            "filename": h.payload.get("filename"),
            "page": h.payload.get("page"),
            "type": h.payload.get("type"),
        }
        for h in hits
    ]


class RagService:
    def __init__(
        self,
        cfg: AppConfig,
        settings: Settings,
        embedder: Embedder,
        sparse: Bm25Embedder,
        store: VectorStore,
        llm: LLM,
        pii: PiiScanner,
        guard: Guard,
        tracer=None,
        prompts: PromptSet | None = None,
    ):
        self.cfg, self.settings = cfg, settings
        self.prompts = prompts or load_prompts(cfg)
        self.embedder, self.sparse, self.store = embedder, sparse, store
        self.llm, self.pii, self.guard = llm, pii, guard
        self.tracer = tracer or NullTracer()
        self._qcache: OrderedDict[str, list[float]] = OrderedDict()
        c = cfg.cache
        self.answers = AnswerCache(c.answer_max_entries, c.answer_ttl_seconds)

    async def _embed_query(self, q: str) -> tuple[list[float], bool]:
        key = normalize_question(q)
        if key in self._qcache:
            self._qcache.move_to_end(key)
            return self._qcache[key], True
        vec = (await self.embedder.embed([q]))[0]
        self._qcache[key] = vec
        if len(self._qcache) > self.cfg.cache.query_embedding_max_entries:
            self._qcache.popitem(last=False)
        return vec, False

    async def _answer_key(self, q: str, document_id: str | None, type_: str | None, not_found: str) -> str:
        """Everything that can change the answer. Add the user's access scope here with access control."""
        return cache_key(
            q=normalize_question(q),
            corpus=await corpus_version(self.settings.db_path),
            prompts=self.prompts.ids,
            llm=self.llm.model,
            rerank_model=self.cfg.llm.rerank_model,
            retrieval=self.cfg.retrieval.model_dump(),
            pii_mode=self.cfg.pii.mode,
            filters={"document_id": document_id, "type": type_},
            not_found=not_found,
            scope="shared",  # every user sees every document today
        )

    def _context(self, hits: list[Hit]) -> str:
        parts = []
        for i, h in enumerate(hits, start=1):
            text = self.guard.sanitize_context(h.payload.get("text", ""))
            parts.append(f"{_tag(h, i)}\n{text}")
        return "\n\n".join(parts)

    def _citations(self, hits: list[Hit], cited: set[int]) -> tuple[list[dict], list[dict]]:
        cites, images = [], []
        for n in sorted(cited):
            p = hits[n - 1].payload
            cites.append(
                {
                    "tag": f"S{n}",
                    "document_id": p.get("document_id"),
                    "filename": p.get("filename"),
                    "page": p.get("page"),
                    "section": p.get("section"),
                    "type": p.get("type"),
                }
            )
            if p.get("asset_path"):
                images.append(
                    {
                        "tag": f"S{n}",
                        "url": signed_url(p["asset_path"]),
                        "page": p.get("page"),
                        "caption": p.get("text", "")[:200],
                    }
                )
        return cites, images

    async def _audit(
        self,
        user: str,
        query: str,
        hits: list[Hit],
        started: float,
        trace_id: str | None,
        prompts: dict[str, str] | None = None,
        cache_hit: bool = False,
    ) -> None:
        mode = self.cfg.pii.mode
        q = await asyncio.to_thread(self.pii.mask, query) if mode != "off" else query
        async with aiosqlite.connect(self.settings.db_path) as db:
            await db.execute(
                "INSERT INTO audit_log (user, query, chunk_ids, model, tokens, latency_ms, trace_id,"
                " prompt_versions, cache_hit) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    user, q, ",".join(h.id for h in hits), self.llm.model, None,
                    int((time.monotonic() - started) * 1000), trace_id,
                    json.dumps(prompts or {}, sort_keys=True), int(cache_hit),
                ),
            )
            await db.commit()

    def _end(self, text, hits, cited, found, trace, trace_id) -> dict:
        cites, images = self._citations(hits, cited) if found else ([], [])
        ev = {
            "type": "final", "answer": text, "found": found, "citations": cites,
            "images": images, "trace_id": trace_id,
        }
        if trace is not None:
            ev["trace"] = trace
        return ev

    @staticmethod
    def _summ(h: Hit) -> dict:
        p = h.payload
        return {
            "filename": p.get("filename"), "page": p.get("page"), "type": p.get("type"),
            "document_id": p.get("document_id"), "text": p.get("text", ""),
        }

    # ------------------------------------------------------------------ public entry
    async def stream(
        self,
        user: str,
        question: str,
        document_id: str | None = None,
        type_: str | None = None,
        trace: bool = False,  # evaluation only: attach what was retrieved to the final event
    ) -> AsyncIterator[dict]:
        root = self.tracer.start(
            "query",
            user_id=user,
            tags=["rag-query"],
            input=self.tracer.text(question),
            metadata={"document_id": document_id, "type": type_, "prompts": self.prompts.ids},
        )
        try:
            async for ev in self._stream(root, user, question, document_id, type_, trace):
                yield ev
        except GuardrailBlocked:
            raise
        except Exception as e:
            root.update(**error_fields(e))
            raise
        finally:
            root.end()

    async def _stream(self, root, user, question, document_id, type_, trace) -> AsyncIterator[dict]:
        started = time.monotonic()
        tr = self.tracer
        trace_id = root.trace_id
        not_found = self.guard.policy.not_found_message
        r = self.cfg.retrieval

        g = root.child("guard")
        try:
            q = self.guard.check_input(question)
        except GuardrailBlocked as e:
            g.update(level="WARNING", status_message=e.code, metadata={"blocked": e.code})
            g.end()
            root.score("blocked", 1.0)
            raise
        g.end()
        if self.cfg.pii.mode == "mask":
            q = await asyncio.to_thread(self.pii.mask, q)  # nothing personal goes to the LLM

        # Answer cache. Evaluation runs (trace=True) always take the full pipeline.
        use_cache = self.cfg.cache.answer_enabled and not trace
        akey = await self._answer_key(q, document_id, type_, not_found) if use_cache else None
        if akey is not None:
            hit = self.answers.get(akey)
            root.score("cache_hit", 1.0 if hit else 0.0)
            if hit is not None:
                root.update(output=tr.text(hit.answer), metadata={"cache_hit": True, "cited": sorted(hit.cited)})
                root.score("found", 1.0)
                await self._audit(user, q, hit.hits, started, trace_id, hit.prompts, cache_hit=True)
                yield {"type": "token", "text": hit.answer}
                yield self._end(hit.answer, hit.hits, hit.cited, True, None, trace_id)  # fresh signed URLs
                return

        with span(root, "embed_query", as_type="embedding", model=getattr(self.embedder, "model", None)) as s:
            dense, cache_hit = await self._embed_query(q)
            sp_idx, sp_val = await asyncio.to_thread(self.sparse.embed_query, q)
            s.update(metadata={"cache_hit": cache_hit, "sparse_terms": len(sp_idx)})

        filters = {"document_id": document_id, "type": type_}
        with span(root, "retrieve", as_type="retriever", input={"filters": filters, "k": r.top_k}) as s:
            res = await self.store.search(dense, sp_idx, sp_val, r.top_k, filters)
            s.update(output=_brief(res.hits), metadata={"top_dense": round(res.top_dense, 4), "n": len(res.hits)})

        raw = [self._summ(h) for h in res.hits] if trace else []
        gated = not res.hits or res.top_dense < r.min_score
        root.score("top_dense", res.top_dense)

        # Relevance gate: weak evidence means no LLM call at all.
        if gated:
            root.score("gated", 1.0)
            root.score("found", 0.0)
            await self._audit(user, q, [], started, trace_id)
            root.update(output=tr.text(not_found), metadata={"gated": True})
            yield {"type": "token", "text": not_found}
            t = {"top_dense": res.top_dense, "retrieved_raw": raw, "retrieved": []} if trace else None
            yield self._end(not_found, [], set(), False, t, trace_id)
            return

        hits = res.hits
        used: dict[str, str] = {}  # prompt IDs this answer actually used (audit log)
        if r.rerank_enabled:
            rp = self.prompts["rerank"]
            used["rerank"] = rp.id
            with span(root, "rerank", as_type="generation", model=self.cfg.llm.rerank_model or self.llm.model,
                      input={"candidates": len(hits)}, metadata={"prompt": rp.id}) as s:
                hits = await rerank(self.llm, self.cfg.llm.rerank_model, q, hits, system=rp.render())
                s.update(output=[h.id for h in hits[: r.rerank_top_n]])
        hits = hits[: r.rerank_top_n]
        t = (
            {"top_dense": res.top_dense, "retrieved_raw": raw, "retrieved": [self._summ(h) for h in hits]}
            if trace
            else None
        )

        ap = self.prompts["answer"]
        used["answer"] = ap.id
        system = ap.render(not_found=not_found)
        prompt = f"Sources:\n\n{self._context(hits)}\n\nQuestion: {q}"
        gen_input = prompt if tr.log_content else {"source_ids": [h.id for h in hits], "question_chars": len(q)}

        streamed: list[str] = []
        with span(root, "generate", as_type="generation", model=self.llm.model, input=gen_input,
                  metadata={"prompt": ap.id}) as gen:
            async for piece in self.llm.stream(system, prompt):
                if not streamed:
                    gen.update(completion_start_time=datetime.now(timezone.utc))  # time to first token
                streamed.append(piece)
                yield {"type": "token", "text": piece}
            answer = "".join(streamed).strip()
            gen.update(output=tr.text(answer))

        regenerated = False
        with span(root, "output_check", as_type="guardrail") as s:
            ok, cited = self.guard.check_output(answer, len(hits))
            first_ok = ok
            if not ok:  # regenerate once with a stricter instruction, then refuse
                regenerated = True
                cp = self.prompts["citation_retry"]
                used["citation_retry"] = cp.id
                retry = await self.llm.generate(
                    system, prompt + "\n\n" + cp.render(n=len(hits), not_found=not_found)
                )
                answer = retry.strip()
                ok, cited = self.guard.check_output(answer, len(hits))
                if not ok:
                    answer, cited = self.guard.policy.refusal_message, set()
                yield {"type": "replace", "text": answer}
            s.update(metadata={"citation_valid": first_ok, "regenerated": regenerated, "cited": sorted(cited)})

        if self.cfg.pii.mode == "mask":
            masked = await asyncio.to_thread(self.pii.mask, answer)
            if masked != answer:
                answer = masked
                yield {"type": "replace", "text": answer}

        found = bool(cited)
        root.score("found", 1.0 if found else 0.0)
        root.score("citation_valid", 1.0 if first_ok else 0.0)
        root.score("regenerated", 1.0 if regenerated else 0.0)
        root.update(output=tr.text(answer), metadata={"n_sources": len(hits), "cited": sorted(cited)})
        await self._audit(user, q, hits, started, trace_id, used)
        if akey is not None and found:  # only answers with valid citations; never not-found or refusals
            self.answers.put(akey, CachedAnswer(answer, hits, set(cited), dict(used)))
        yield self._end(answer, hits, cited, found, t, trace_id)
