"""Query-embedding cache key normalization and the exact-match answer cache."""
import asyncio
import shutil
import sqlite3

import pytest

from app.db import corpus_version
from app.prompts import load_prompts
from app.retrieval.answer_cache import AnswerCache, CachedAnswer, normalize_question
from app.settings import get_settings
from tests.conftest import token
from tests.test_chat_api import ask
from tests.test_ingestion_api import up

Q = "How long do refunds take?"


@pytest.fixture
def docs(api):
    """Two documents; returns (viewer headers, ingestor headers, {filename: document_id})."""
    ing = token(api, "ingest", "ingest-password-1")
    files = {
        "refunds.md": b"# Refunds\nRefunds are paid within 14 days.\n",
        "fees.md": b"# Fees\nThe monthly fee is 5 dollars.\n",
    }
    ids = {name: up(api, ing, name, data).json()["document_id"] for name, data in files.items()}
    return token(api, "viewer", "viewer-password-1"), ing, ids


def audit_cache_flags():
    con = sqlite3.connect(get_settings().db_path)
    return [r[0] for r in con.execute("SELECT cache_hit FROM audit_log ORDER BY id")]


# ------------------------------------------------------------------ unit
@pytest.mark.parametrize(
    "variant", ["how long do refunds take", "  HOW long do   refunds take?? ", "How long do refunds take."]
)
def test_normalize_question_merges_trivial_variants(variant):
    assert normalize_question(variant) == normalize_question(Q)


def test_normalize_question_keeps_real_differences():
    assert normalize_question("How long do refunds take?") != normalize_question("How long do fees take?")
    assert normalize_question("?") == "?"  # never an empty key


def test_answer_cache_ttl_and_lru():
    now = [0.0]
    c = AnswerCache(max_entries=2, ttl_seconds=10, clock=lambda: now[0])
    c.put("a", CachedAnswer("A", [], {1}, {}))
    c.put("b", CachedAnswer("B", [], {1}, {}))
    assert c.get("a").answer == "A"           # 'a' is now most recently used
    c.put("c", CachedAnswer("C", [], {1}, {}))  # evicts 'b'
    assert c.get("b") is None and c.get("c").answer == "C"
    now[0] = 11
    assert c.get("a") is None and len(c) == 1  # expired and removed
    assert (c.hits, c.misses) == (2, 2)


# ------------------------------------------------------------------ query-embedding cache
def test_embedding_cache_uses_normalized_key(docs, api, rag, parts, monkeypatch):
    monkeypatch.setattr(rag.cfg.cache, "answer_enabled", False)  # isolate the embedding cache
    viewer, _, _ = docs
    embedder = parts[1]
    ask(api, viewer, Q)
    ask(api, viewer, "  how LONG do refunds take ")
    assert sum(1 for t in embedder.texts_embedded if "refunds take" in t.lower()) == 1


# ------------------------------------------------------------------ answer cache
def test_repeat_question_served_from_cache(docs, api, llm, tracer):
    viewer, _, _ = docs
    first = ask(api, viewer, Q).json()
    calls = (len(llm.prompts), llm.rerank_calls)
    second = ask(api, viewer, "how long do refunds take").json()
    assert (len(llm.prompts), llm.rerank_calls) == calls  # no search, rerank or generation
    assert second["answer"] == first["answer"] and second["citations"] == first["citations"]
    assert second["trace_id"] != first["trace_id"]  # still its own trace
    assert audit_cache_flags() == [0, 1]           # both requests audited (and rate limited)
    assert [v for _, n, v, _ in tracer.scores if n == "cache_hit"] == [0.0, 1.0]


def test_streaming_endpoint_uses_cache(docs, api, llm):
    viewer, _, _ = docs
    ask(api, viewer, Q)
    n = len(llm.prompts)
    r = api.post("/chat/stream", json={"question": Q}, headers=viewer)
    assert r.status_code == 200 and "event: final" in r.text and len(llm.prompts) == n


def test_new_upload_invalidates_cache(docs, api, llm):
    viewer, ing, _ = docs
    before = asyncio.run(corpus_version(get_settings().db_path))
    ask(api, viewer, Q)
    up(api, ing, "faq.md", b"# FAQ\nRefunds can also be requested by phone.\n")
    assert asyncio.run(corpus_version(get_settings().db_path)) == before + 1
    n = len(llm.prompts)
    ask(api, viewer, Q)
    assert len(llm.prompts) == n + 1 and audit_cache_flags() == [0, 0]


def test_delete_invalidates_cache(docs, api, llm):
    viewer, _, ids = docs
    ask(api, viewer, Q)
    admin = token(api, "admin", "admin-password-1")
    assert api.delete(f"/documents/{ids['fees.md']}", headers=admin).status_code == 204
    n = len(llm.prompts)
    ask(api, viewer, Q)
    assert len(llm.prompts) == n + 1


def test_failed_ingestion_invalidates_cache(docs, api):
    _, ing, _ = docs
    before = asyncio.run(corpus_version(get_settings().db_path))
    up(api, ing, "blank.md", b"  \n")  # no content: ingestion fails
    assert asyncio.run(corpus_version(get_settings().db_path)) == before + 1


def test_not_found_is_not_cached(docs, api, rag, parts):
    viewer, _, _ = docs
    parts[3].top_dense = 0.01  # relevance gate: "not found"
    ask(api, viewer, "Weather on Mars?")
    ask(api, viewer, "Weather on Mars?")
    assert len(rag.answers) == 0 and audit_cache_flags() == [0, 0]


def test_refusal_is_not_cached(docs, api, rag, llm):
    viewer, _, _ = docs
    llm.answer = llm.retry_answer = "No citations here."  # fails twice: refusal
    body = ask(api, viewer, Q).json()
    assert body["found"] is False and len(rag.answers) == 0


def test_filters_are_part_of_the_key(docs, api, llm):
    viewer, _, ids = docs
    ask(api, viewer, Q)
    ask(api, viewer, Q, document_id=ids["refunds.md"])
    ask(api, viewer, Q, type="text")
    assert audit_cache_flags() == [0, 0, 0]


def test_prompt_change_is_part_of_the_key(docs, api, rag, tmp_path):
    viewer, _, _ = docs
    ask(api, viewer, Q)
    d = tmp_path / "prompts"
    shutil.copytree("prompts", d)
    (d / "answer" / "v2.yaml").write_text(
        'name: answer\nversion: 2\nvariables: [not_found]\ntemplate: "Use sources. Else: {not_found}"\n',
        encoding="utf-8",
    )
    cfg = rag.cfg.model_copy(deep=True)
    cfg.prompts.dir, cfg.prompts.answer = str(d), 2
    rag.prompts = load_prompts(cfg)
    ask(api, viewer, Q)
    assert audit_cache_flags() == [0, 0]


def test_evaluation_runs_bypass_cache(docs, rag, llm):
    async def run():
        for _ in range(2):
            [ev async for ev in rag.stream("eval", Q, trace=True)]

    asyncio.run(run())
    assert len(llm.prompts) == 2 and len(rag.answers) == 0


def test_cache_can_be_disabled(docs, api, rag, llm, monkeypatch):
    monkeypatch.setattr(rag.cfg.cache, "answer_enabled", False)
    viewer, _, _ = docs
    ask(api, viewer, Q)
    ask(api, viewer, Q)
    assert len(llm.prompts) == 2 and len(rag.answers) == 0
