import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import settings as st
from app.auth import hash_password
from app.db import init_db
from app.vectorstore import Point


class FakeEmbedder:
    model, dimensions = "fake", 8

    def __init__(self):
        self.texts_embedded: list[str] = []

    async def embed(self, texts):
        self.texts_embedded += texts
        return [[1.0] + [0.0] * 7 for _ in texts]


class FakeVision:
    def __init__(self):
        self.calls = 0

    async def caption(self, image_bytes, mime, prompt):
        self.calls += 1
        return f"A chart showing revenue, image of {len(image_bytes)} bytes"


class FakeSparse:
    def embed(self, texts):
        return [([1], [1.0]) for _ in texts]

    def embed_query(self, text):
        return [1], [1.0]


class FakeLLM:
    model = "fake-llm"

    def __init__(self):
        self.answer = "Refunds are paid within 14 days [S1]."
        self.retry_answer = None  # answer used when the first one is regenerated
        self.prompts: list[str] = []
        self.rerank_calls = 0
        self.generate_calls = 0

    async def generate(self, system, user, max_tokens=800, model=None):
        if "ranking function" in system:
            self.rerank_calls += 1
            return "[1, 0]"
        self.generate_calls += 1
        self.prompts.append(user)
        return self.retry_answer if self.retry_answer is not None else self.answer

    async def stream(self, system, user, max_tokens=800):
        self.prompts.append(user)
        for w in self.answer.split(" "):
            yield w + " "


class FakePii:
    def scan(self, text):
        return ["EMAIL_ADDRESS"] if "@" in text else []

    def mask(self, text):
        return text.replace("@", " at ")


class MemStore:
    def __init__(self):
        self.points: dict[str, Point] = {}
        self.top_dense = 0.9  # tests lower this to exercise the relevance gate
        self.last_filters = None

    async def search(self, dense, sp_idx, sp_val, limit, filters):
        from app.vectorstore import Hit, SearchResult

        self.last_filters = filters
        hits = [
            Hit(i, 1.0 / (n + 1), dict(p.payload))
            for n, (i, p) in enumerate(self.points.items())
            if all(v is None or p.payload.get(k) == v for k, v in filters.items())
        ][:limit]
        return SearchResult(hits, self.top_dense if hits else 0.0)

    async def ensure(self):
        pass

    async def existing_ids(self, ids):
        return {i for i in ids if i in self.points}

    async def upsert(self, points):
        for p in points:
            self.points[p.id] = p

    async def update_payload(self, point_id, payload):
        self.points[point_id].payload.update(payload)

    async def doc_point_ids(self, document_id):
        return {i for i, p in self.points.items() if p.payload["document_id"] == document_id}

    async def delete(self, ids):
        for i in ids:
            self.points.pop(i, None)

    async def delete_document(self, document_id):
        for i in await self.doc_point_ids(document_id):
            self.points.pop(i)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("CONFIG_PATH", "config/config.yaml")
    st.get_settings.cache_clear()
    st.get_config.cache_clear()
    asyncio.run(init_db())
    return tmp_path


class FakeSpan:
    def __init__(self, tracer, trace_id, name):
        self.tracer, self.trace_id, self.name = tracer, trace_id, name

    def child(self, name, as_type="span", **kw):
        self.tracer.spans.append((name, as_type))
        return FakeSpan(self.tracer, self.trace_id, name)

    def update(self, **kw):
        self.tracer.updates.append((self.name, kw))

    def end(self):
        pass

    def score(self, name, value, comment=None):
        self.tracer.scores.append((self.trace_id, name, value, comment))


class FakeTracer:
    """Records what the pipeline reports; ids look like real trace ids."""

    enabled, log_content, environment = True, False, "test"

    def __init__(self):
        self.spans, self.updates, self.scores, self.roots = [], [], [], []

    def start(self, name, **kw):
        import uuid

        self.roots.append((name, kw))
        return FakeSpan(self, uuid.uuid4().hex, name)

    def score(self, trace_id, name, value, comment=None):
        self.scores.append((trace_id, name, value, comment))

    def text(self, s):
        return f"[{len(s)} chars]"

    def flush(self):
        pass

    def shutdown(self):
        pass


@pytest.fixture
def tracer():
    return FakeTracer()


@pytest.fixture
def parts(env, tracer):
    from app.ingestion.pipeline import Ingestor

    embedder, vision, store = FakeEmbedder(), FakeVision(), MemStore()
    ing = Ingestor(
        st.get_config(), st.get_settings(), embedder, vision, store, FakeSparse(), FakePii(), tracer
    )
    return ing, embedder, vision, store


@pytest.fixture
def llm():
    return FakeLLM()


@pytest.fixture
def rag(env, parts, llm, tracer):
    from app.guardrails import Guard, get_policy
    from app.retrieval.service import RagService

    _, embedder, _, store = parts
    return RagService(
        st.get_config(), st.get_settings(), embedder, FakeSparse(), store, llm, FakePii(),
        Guard(get_policy()), tracer,
    )


@pytest.fixture
def api(env, parts, rag):
    from app.deps import get_ingestor, get_rag, get_store
    from app.main import app

    ing, _, _, store = parts
    app.dependency_overrides[get_ingestor] = lambda: ing
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_rag] = lambda: rag
    con = sqlite3.connect(env / "app.db")
    con.executemany(
        "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
        [
            ("admin", hash_password("admin-password-1"), "admin"),
            ("ingest", hash_password("ingest-password-1"), "ingestor"),
            ("viewer", hash_password("viewer-password-1"), "user"),
        ],
    )
    con.commit()
    con.close()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def token(client, user, pw):
    r = client.post("/auth/login", data={"username": user, "password": pw})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
