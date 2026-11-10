"""Prompt versioning: files, IDs, config switch, and where the IDs are recorded."""
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.prompts import PROMPT_NAMES, Prompt, PromptError, load_prompt, load_prompts
from app.settings import get_config, get_settings
from tests.conftest import token
from tests.test_chat_api import ask, loaded  # noqa: F401  (fixture)
from tests.test_ingestion_api import png, up

ID = re.compile(r"^[a-z_]+@v\d+#[0-9a-f]{8}$")


def _copy_prompts(tmp_path: Path) -> Path:
    dst = tmp_path / "prompts"
    shutil.copytree("prompts", dst)
    return dst


def _write(d: Path, name: str, version: int, template: str, variables: list[str]) -> None:
    (d / name / f"v{version}.yaml").write_text(
        f"name: {name}\nversion: {version}\nvariables: {json.dumps(variables)}\n"
        f"template: {json.dumps(template)}\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------ files and IDs
def test_active_prompts_load_with_ids(env):
    ids = load_prompts(get_config()).ids
    assert set(ids) == set(PROMPT_NAMES)
    assert all(ID.match(v) for v in ids.values()), ids


def test_id_changes_when_text_changes_without_version_bump():
    a = Prompt("answer", 1, "Use the sources. {not_found}", ("not_found",))
    b = Prompt("answer", 1, "Use ONLY the sources. {not_found}", ("not_found",))
    assert a.id != b.id and a.id.startswith("answer@v1#")


def test_render_requires_exact_variables():
    p = Prompt("citation_retry", 1, "S1 to S{n}: {not_found}", ("n", "not_found"))
    assert p.render(n=3, not_found="NF") == "S1 to S3: NF"
    with pytest.raises(PromptError):
        p.render(n=3)
    with pytest.raises(PromptError):
        p.render(n=3, not_found="NF", extra="x")


def test_template_without_variables_keeps_braces_literal():
    p = Prompt("rerank", 1, "Output a JSON array like [1, 0]; {not a field}", ())
    assert p.render() == p.template


def test_load_rejects_placeholder_mismatch(tmp_path):
    d = _copy_prompts(tmp_path)
    _write(d, "answer", 2, "Reply {not_found} or {oops}", ["not_found"])
    with pytest.raises(PromptError, match="placeholders"):
        load_prompt(d, "answer", 2)


def test_load_rejects_missing_version_and_wrong_header(tmp_path):
    d = _copy_prompts(tmp_path)
    with pytest.raises(PromptError, match="not found"):
        load_prompt(d, "answer", 99)
    (d / "answer" / "v3.yaml").write_text("name: answer\nversion: 2\ntemplate: x\n", encoding="utf-8")
    with pytest.raises(PromptError, match="name/version"):
        load_prompt(d, "answer", 3)


# ------------------------------------------------------------------ switching versions
class _SystemSpy:
    """Wraps the fake LLM and records every system prompt it receives."""

    def __init__(self, llm):
        self._llm, self.systems = llm, []
        self.model = llm.model

    async def generate(self, system, user, **kw):
        self.systems.append(system)
        return await self._llm.generate(system, user, **kw)

    async def stream(self, system, user, **kw):
        self.systems.append(system)
        async for piece in self._llm.stream(system, user, **kw):
            yield piece


def test_config_selects_answer_version(env, parts, llm, tracer, tmp_path):
    from app.guardrails import Guard, get_policy
    from app.retrieval.service import RagService
    from tests.conftest import FakePii, FakeSparse

    ing, embedder, _, store = parts
    d = _copy_prompts(tmp_path)
    _write(d, "answer", 2, "V2 ANSWER PROMPT. Otherwise say: {not_found}", ["not_found"])
    cfg = get_config().model_copy(deep=True)
    cfg.prompts.dir, cfg.prompts.answer = str(d), 2

    import asyncio

    con = sqlite3.connect(get_settings().db_path)
    con.execute(
        "INSERT INTO documents (document_id, version, filename, file_sha256, raw_path) VALUES (?,?,?,?,?)",
        ("d1", 1, "r.md", "x", "raw/d1/v1_r.md"),
    )
    con.commit()
    raw = Path(get_settings().data_dir) / "raw" / "d1"
    raw.mkdir(parents=True)
    (raw / "v1_r.md").write_text("# Refunds\nRefunds are paid within 14 days.\n", encoding="utf-8")
    asyncio.run(ing.run(con.execute("SELECT id FROM documents").fetchone()[0]))
    con.close()

    spy = _SystemSpy(llm)
    svc = RagService(cfg, get_settings(), embedder, FakeSparse(), store, spy, FakePii(), Guard(get_policy()),
                     tracer, prompts=load_prompts(cfg))

    async def run():
        return [ev async for ev in svc.stream("viewer", "How long do refunds take?")]

    asyncio.run(run())
    answer_system = [s for s in spy.systems if s.startswith("V2 ANSWER PROMPT")]
    assert answer_system and get_policy().not_found_message in answer_system[0]
    meta = tracer.roots[-1][1]["metadata"]["prompts"]
    assert meta["answer"].startswith("answer@v2#")


# ------------------------------------------------------------------ where IDs are recorded
def test_audit_log_records_prompt_versions(api, loaded, llm):  # noqa: F811
    ask(api, loaded, "How long do refunds take?")
    row = sqlite3.connect(get_settings().db_path).execute("SELECT prompt_versions FROM audit_log").fetchone()
    used = json.loads(row[0])
    ids = load_prompts(get_config()).ids
    assert used == {"answer": ids["answer"], "rerank": ids["rerank"]}  # no citation retry needed


def test_audit_log_records_citation_retry_when_used(api, loaded, llm):  # noqa: F811
    llm.answer = "Refunds take 14 days."  # no citation: forces the retry prompt
    llm.retry_answer = "Refunds take 14 days [S1]."
    ask(api, loaded, "How long do refunds take?")
    row = sqlite3.connect(get_settings().db_path).execute("SELECT prompt_versions FROM audit_log").fetchone()
    assert json.loads(row[0])["citation_retry"] == load_prompts(get_config()).ids["citation_retry"]


def test_gated_answer_records_no_prompts(api, loaded, parts):  # noqa: F811
    parts[3].top_dense = 0.01
    ask(api, loaded, "Weather on Mars?")
    row = sqlite3.connect(get_settings().db_path).execute("SELECT prompt_versions FROM audit_log").fetchone()
    assert json.loads(row[0]) == {}


def test_trace_and_spans_carry_prompt_ids(api, loaded, tracer):  # noqa: F811
    ask(api, loaded, "How long do refunds take?")
    query_roots = [kw for name, kw in tracer.roots if name == "query"]
    assert set(query_roots[-1]["metadata"]["prompts"]) == set(PROMPT_NAMES)


def test_image_chunk_records_caption_prompt(api, parts):
    _, _, _, store = parts
    up(api, token(api, "ingest", "ingest-password-1"), "chart.png", png())
    p = next(iter(store.points.values()))
    assert p.payload["caption_prompt"] == load_prompts(get_config()).ids["caption"]


def test_text_chunk_has_no_caption_prompt(api, parts):
    _, _, _, store = parts
    up(api, token(api, "ingest", "ingest-password-1"), "a.md", b"# A\nSome text.\n")
    assert next(iter(store.points.values())).payload["caption_prompt"] is None


def test_new_caption_prompt_recaptions_cached_image(env, parts, tmp_path):
    import asyncio

    from app.ingestion.parsers import Block

    ing, _, vision, _ = parts
    assets = Path(get_settings().data_dir) / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "i.png").write_bytes(png())
    sem = asyncio.Semaphore(1)

    def caption():
        b = Block(kind="image", text="", page=1, asset_path="assets/i.png", mime="image/png")
        return asyncio.run(ing._caption(b, sem))

    assert caption() is False and caption() is True  # second time comes from the cache
    assert vision.calls == 1

    d = _copy_prompts(tmp_path)
    _write(d, "caption", 2, "Describe the image in detail.", [])
    ing.caption_prompt = load_prompt(d, "caption", 2)
    assert caption() is False  # new prompt: cache miss, real vision call
    assert vision.calls == 2


def test_eval_report_includes_prompt_ids(env):
    from evaluation.runner import build_report

    report = build_report(get_config(), [], [])
    assert report["config"]["prompts"] == load_prompts(get_config()).ids
