"""Startup sweep for interrupted ingestions, and re-indexing from raw files."""
import asyncio
import sqlite3
import sys

from fastapi.testclient import TestClient

from app.db import INTERRUPTED, corpus_version, mark_interrupted
from app.ingestion.reindex import reindex, select_targets
from app.settings import get_settings
from tests.conftest import token
from tests.test_ingestion_api import up

REFUNDS = b"# Refunds\nRefunds are paid within 14 days.\n"
FEES = b"# Fees\nThe monthly fee is 5 dollars.\n"


def db():
    return sqlite3.connect(get_settings().db_path)


def set_status(document_id, version, status):
    con = db()
    con.execute("UPDATE documents SET status=? WHERE document_id=? AND version=?", (status, document_id, version))
    con.commit()
    con.close()


def statuses(document_id):
    con = db()
    rows = con.execute(
        "SELECT version, status FROM documents WHERE document_id=? ORDER BY version", (document_id,)
    ).fetchall()
    con.close()
    return rows


def run(coro):
    return asyncio.run(coro)


def version():
    return run(corpus_version(get_settings().db_path))


def ingest(api, name, data):
    return up(api, token(api, "ingest", "ingest-password-1"), name, data).json()


# ------------------------------------------------------------------ startup sweep
def test_mark_interrupted_only_touches_unfinished_rows(env):
    con = db()
    con.executemany(
        "INSERT INTO documents (document_id, version, filename, file_sha256, status) VALUES (?,?,?,?,?)",
        [("a", 1, "a.md", "1", "pending"), ("b", 1, "b.md", "2", "processing"),
         ("c", 1, "c.md", "3", "ready"), ("d", 1, "d.md", "4", "failed")],
    )
    con.commit()
    before = version()
    assert run(mark_interrupted(get_settings().db_path)) == 2
    rows = dict(con.execute("SELECT document_id, status FROM documents").fetchall())
    assert rows == {"a": "failed", "b": "failed", "c": "ready", "d": "failed"}
    assert con.execute("SELECT error FROM documents WHERE document_id='b'").fetchone()[0] == INTERRUPTED
    assert version() == before + 1  # partial chunks may exist: cached answers invalidated
    assert run(mark_interrupted(get_settings().db_path)) == 0 and version() == before + 1


def test_restart_unblocks_reupload_of_interrupted_file(api):
    doc = ingest(api, "refunds.md", REFUNDS)["document_id"]
    set_status(doc, 1, "processing")  # simulate a crash mid-ingestion
    assert ingest(api, "refunds.md", REFUNDS)["status"] == "duplicate"  # blocked before the fix
    from app.main import app

    with TestClient(app):  # server restart: lifespan runs the sweep
        pass
    assert statuses(doc) == [(1, "failed")]
    r = ingest(api, "refunds.md", REFUNDS)
    assert r["status"] == "pending" and r["version"] == 2
    assert statuses(doc) == [(1, "failed"), (2, "ready")]


# ------------------------------------------------------------------ choosing what to reindex
def test_select_all_prefers_newest_ready_version(api):
    doc = ingest(api, "refunds.md", REFUNDS)["document_id"]
    ingest(api, "refunds.md", REFUNDS + b"More text.\n")       # v2 ready, v1 superseded
    ingest(api, "refunds.md", b"   \n")                        # v3 fails: no content
    assert statuses(doc) == [(1, "superseded"), (2, "ready"), (3, "failed")]
    t = run(select_targets(get_settings().db_path, "all"))
    assert [(x.document_id, x.version) for x in t] == [(doc, 2)]
    f = run(select_targets(get_settings().db_path, "failed"))
    assert [(x.document_id, x.version) for x in f] == [(doc, 3)]


def test_select_failed_ignores_failures_already_replaced(api):
    doc = ingest(api, "fees.md", b"  \n")["document_id"]        # v1 failed
    ingest(api, "fees.md", FEES)                                # v2 ready
    assert statuses(doc) == [(1, "failed"), (2, "ready")]
    assert run(select_targets(get_settings().db_path, "failed")) == []


def test_select_one_document(api):
    a = ingest(api, "refunds.md", REFUNDS)["document_id"]
    ingest(api, "fees.md", FEES)
    t = run(select_targets(get_settings().db_path, "document", a))
    assert [x.document_id for x in t] == [a]


# ------------------------------------------------------------------ reindexing
def test_reindex_all_rebuilds_lost_collection(api, parts):
    ing, embedder, _, store = parts
    ingest(api, "refunds.md", REFUNDS)
    ingest(api, "fees.md", FEES)
    before_points = set(store.points)
    store.points.clear()                                        # Qdrant collection lost
    n_embedded = len(embedder.texts_embedded)
    s = get_settings()
    results = run(reindex(ing, s.db_path, s.data_dir, run(select_targets(s.db_path, "all"))))
    assert [r["result"] for r in results] == ["ready", "ready"]
    assert set(store.points) == before_points                   # same content-based IDs
    assert len(embedder.texts_embedded) > n_embedded


def test_reindex_unchanged_documents_embeds_nothing(api, parts):
    ing, embedder, _, _ = parts
    ingest(api, "refunds.md", REFUNDS)
    n = len(embedder.texts_embedded)
    s = get_settings()
    run(reindex(ing, s.db_path, s.data_dir, run(select_targets(s.db_path, "all"))))
    assert len(embedder.texts_embedded) == n


def test_reindex_failed_recovers_interrupted_document(api, parts):
    ing, _, _, store = parts
    doc = ingest(api, "refunds.md", REFUNDS)["document_id"]
    set_status(doc, 1, "processing")
    store.points.clear()
    run(mark_interrupted(get_settings().db_path))
    s = get_settings()
    v = version()
    results = run(reindex(ing, s.db_path, s.data_dir, run(select_targets(s.db_path, "failed"))))
    assert results[0]["result"] == "ready" and results[0]["error"] is None
    assert statuses(doc) == [(1, "ready")] and store.points
    assert version() > v                                        # cached answers invalidated


def test_reindex_skips_missing_raw_file_and_dry_run_changes_nothing(api, parts):
    ing, embedder, _, _ = parts
    a = ingest(api, "refunds.md", REFUNDS)["document_id"]
    ingest(api, "fees.md", FEES)
    s = get_settings()
    con = db()
    raw = con.execute("SELECT raw_path FROM documents WHERE document_id=?", (a,)).fetchone()[0]
    con.close()
    (s.db_path.parent / raw).unlink()
    targets = run(select_targets(s.db_path, "all"))
    n = len(embedder.texts_embedded)
    dry = run(reindex(ing, s.db_path, s.data_dir, targets, dry_run=True))
    assert sorted(r["result"] for r in dry) == ["skipped", "would_reindex"]
    assert len(embedder.texts_embedded) == n
    real = run(reindex(ing, s.db_path, s.data_dir, targets))
    assert sorted(r["result"] for r in real) == ["ready", "skipped"]


def test_cli_dry_run(api, monkeypatch, capsys):
    ingest(api, "refunds.md", REFUNDS)
    from scripts import reindex as cli

    monkeypatch.setattr(sys, "argv", ["reindex", "--all", "--dry-run"])
    assert run(cli.main()) == 0
    out = capsys.readouterr().out
    assert "1 document(s) to check" in out and '"would_reindex"' in out
