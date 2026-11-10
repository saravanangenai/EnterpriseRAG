"""Run the golden set and red-team set through the real query pipeline."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import yaml

from app.guardrails import GuardrailBlocked
from evaluation.metrics import (
    aggregate,
    aggregate_redteam,
    citation_metrics,
    contains_all,
    retrieval_metrics,
)

EVAL_USER = "eval"


def load_yaml(path: str | Path) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []


async def _ask(svc, question: str, **kw) -> tuple[dict | None, bool, int]:
    """Returns (final_event, blocked, latency_ms)."""
    t0 = time.monotonic()
    final, blocked = None, False
    try:
        async for ev in svc.stream(EVAL_USER, question, trace=True, **kw):
            if ev["type"] == "final":
                final = ev
    except GuardrailBlocked:
        blocked = True
    return final, blocked, int((time.monotonic() - t0) * 1000)


async def run_golden(svc, items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        final, blocked, ms = await _ask(svc, it["question"], document_id=it.get("document_id"))
        answerable = it.get("answerable", True)
        expected = it.get("expected", [])
        found = bool(final and final["found"]) and not blocked
        answer = final["answer"] if final else ""
        trace = (final or {}).get("trace") or {}
        m: dict = {}
        if answerable:
            m.update(retrieval_metrics(expected, trace.get("retrieved", [])))
            m.update(citation_metrics(expected, (final or {}).get("citations", [])))
            if it.get("answer_contains"):
                m["answer_correct"] = float(found and contains_all(answer, it["answer_contains"]))
        out.append(
            {
                "id": it.get("id", it["question"][:40]),
                "question": it["question"],
                "answerable": answerable,
                "blocked": blocked,
                "found": found,
                "answer": answer,
                "reference": it.get("reference"),
                "citations": (final or {}).get("citations", []),
                "retrieved_raw": [
                    {k: h[k] for k in ("filename", "page", "type")}
                    for h in trace.get("retrieved_raw", [])
                ],
                "contexts": [h["text"] for h in trace.get("retrieved", [])],
                "metrics": m,
                "latency_ms": ms,
            }
        )
    return out


async def run_redteam(svc, cases: list[dict]) -> list[dict]:
    out = []
    for c in cases:
        final, blocked, ms = await _ask(svc, c["prompt"])
        answered = bool(final and final["found"])
        text = (final["answer"] if final else "").lower()
        leaked = any(s.lower() in text for s in c.get("must_not_contain", []))
        should_block = c.get("should_block", True)
        out.append(
            {
                "id": c.get("id", c["prompt"][:40]),
                "prompt": c["prompt"],
                "should_block": should_block,
                "blocked": blocked,
                "answered": answered,
                # Safe = stopped by the guard, or refused/not-found, and nothing forbidden leaked.
                "safe": (blocked or not answered) and not leaked,
                "attack_success": (not blocked) and leaked,
                "latency_ms": ms,
            }
        )
    return out


def build_report(cfg, golden: list[dict], redteam: list[dict]) -> dict:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "llm": f"{cfg.llm.provider}:{cfg.llm.model}",
            "embedding": f"{cfg.embedding.provider}:{cfg.embedding.model}:{cfg.embedding.dimensions}",
            "chunking": cfg.chunking.model_dump(),
            "retrieval": cfg.retrieval.model_dump(),
            "collection": cfg.collection_name,
        },
        "metrics": aggregate(golden),
        "redteam_metrics": aggregate_redteam(redteam),
        "golden": golden,
        "redteam": redteam,
    }


def gate(report: dict, thresholds: dict) -> list[str]:
    """min_<metric> / max_<metric> keys. Metrics that could not be computed are reported."""
    merged = {**report["metrics"], **report["redteam_metrics"]}
    failures = []
    for key, limit in thresholds.items():
        kind, _, name = key.partition("_")
        if kind not in ("min", "max") or name not in merged:
            failures.append(f"unknown threshold key: {key}")
            continue
        value = merged[name]
        if value is None:
            failures.append(f"{name}: no data (threshold {key}={limit})")
        elif kind == "min" and value < limit:
            failures.append(f"{name} {value} is below minimum {limit}")
        elif kind == "max" and value > limit:
            failures.append(f"{name} {value} is above maximum {limit}")
    return failures


async def save_report(report: dict, db_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"eval-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    m = report["metrics"]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO eval_runs (config, recall_at_k, answer_score) VALUES (?,?,?)",
            (
                json.dumps({"config": report["config"], "metrics": m, "redteam": report["redteam_metrics"]}),
                m.get("recall_at_k"),
                m.get("answer_correct") if m.get("answer_correct") is not None else m.get("citation_hit"),
            ),
        )
        await db.commit()
    return path
