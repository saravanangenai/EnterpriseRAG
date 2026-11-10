import io

from PIL import Image

from tests.conftest import token


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), (10, 80, 200)).save(buf, "PNG")
    return buf.getvalue()


def up(api, headers, name, data: bytes):
    return api.post("/documents", files={"file": (name, data)}, headers=headers)


def status_of(api, headers, document_id):
    return api.get(f"/documents/{document_id}", headers=headers).json()["versions"]


def test_upload_requires_ingestor_or_admin(api):
    viewer = token(api, "viewer", "viewer-password-1")
    assert up(api, viewer, "a.md", b"# Hi\ntext").status_code == 403
    assert api.post("/documents", files={"file": ("a.md", b"x")}).status_code == 401


def test_rejects_unsupported_and_empty(api):
    h = token(api, "ingest", "ingest-password-1")
    assert up(api, h, "virus.exe", b"MZ").status_code == 415
    assert up(api, h, "empty.md", b"").status_code == 422


def test_upload_ingests_and_marks_ready(api, parts):
    _, embedder, _, store = parts
    h = token(api, "ingest", "ingest-password-1")
    r = up(api, h, "policy.md", b"# Refunds\nRefunds are paid within 14 days.\n")
    assert r.status_code == 202 and r.json()["version"] == 1
    v = status_of(api, h, r.json()["document_id"])[0]
    assert v["status"] == "ready" and v["chunk_count"] == 1
    point = next(iter(store.points.values()))
    assert point.payload["filename"] == "policy.md"
    assert point.payload["access_group"] is None


def test_same_bytes_are_skipped(api, parts):
    _, embedder, _, _ = parts
    h = token(api, "ingest", "ingest-password-1")
    data = b"# T\nSome content that is long enough to matter.\n"
    up(api, h, "a.md", data)
    n = len(embedder.texts_embedded)
    r = up(api, h, "renamed.md", data)  # identical bytes under another name
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(embedder.texts_embedded) == n


def test_edit_creates_version_reembeds_only_changes_and_removes_stale(api, parts):
    _, embedder, _, store = parts
    h = token(api, "ingest", "ingest-password-1")
    v1 = "# A\nFirst section stays the same.\n\n# B\nOld wording about fees.\n"
    v2 = "# A\nFirst section stays the same.\n\n# B\nNew wording about fees.\n"
    r1 = up(api, h, "resume.md", v1.encode())
    doc = r1.json()["document_id"]
    assert len(store.points) == 2
    embedded_after_v1 = len(embedder.texts_embedded)

    r2 = up(api, h, "resume.md", v2.encode())
    assert r2.json()["version"] == 2 and r2.json()["document_id"] == doc
    assert len(embedder.texts_embedded) - embedded_after_v1 == 1  # only the changed section
    texts = " ".join(p.payload["text"] for p in store.points.values())
    assert "New wording" in texts and "Old wording" not in texts
    assert len(store.points) == 2
    versions = status_of(api, h, doc)
    assert [x["status"] for x in versions] == ["ready", "superseded"]


def test_image_is_captioned_once_and_cached(api, parts):
    _, _, vision, store = parts
    h = token(api, "ingest", "ingest-password-1")
    up(api, h, "chart.png", png())
    assert vision.calls == 1
    p = next(iter(store.points.values()))
    assert p.payload["type"] == "image" and "revenue" in p.payload["text"]
    assert p.payload["asset_path"].startswith("assets/")
    up(api, h, "chart_copy.png", png() + b"")  # same bytes: duplicate, no new caption
    assert vision.calls == 1


def test_pii_flagged_in_payload(api, parts):
    _, _, _, store = parts
    h = token(api, "ingest", "ingest-password-1")
    up(api, h, "c.md", b"# Contact\nEmail me at jane@example.com for details.\n")
    p = next(iter(store.points.values()))
    assert p.payload["pii_flags"] == ["EMAIL_ADDRESS"]


def test_empty_content_marks_failed(api):
    h = token(api, "ingest", "ingest-password-1")
    r = up(api, h, "blank.md", b"   \n  \n")
    v = status_of(api, h, r.json()["document_id"])[0]
    assert v["status"] == "failed" and "No extractable content" in v["error"]


def test_delete_is_admin_only_and_removes_everything(api, parts):
    _, _, _, store = parts
    ing = token(api, "ingest", "ingest-password-1")
    adm = token(api, "admin", "admin-password-1")
    doc = up(api, ing, "d.md", b"# X\nsome text here\n").json()["document_id"]
    assert api.delete(f"/documents/{doc}", headers=ing).status_code == 403
    assert api.delete(f"/documents/{doc}", headers=adm).status_code == 204
    assert not store.points
    assert api.get(f"/documents/{doc}", headers=adm).status_code == 404
