"""CLI. From the project root:

    python -m evaluation.run --golden evaluation/golden.yaml --redteam evaluation/redteam.yaml

Exit code 1 when a threshold in evaluation/thresholds.yaml is missed (use it as a CI gate).
Needs real keys and an ingested corpus, because it runs the real pipeline.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.settings import get_config, get_settings
from evaluation.runner import (
    build_report,
    gate,
    load_yaml,
    run_golden,
    run_redteam,
    save_report,
)


async def run_cli(svc, argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default="evaluation/golden.yaml")
    ap.add_argument("--redteam", default="evaluation/redteam.yaml")
    ap.add_argument("--thresholds", default="evaluation/thresholds.yaml")
    ap.add_argument("--limit", type=int, help="only the first N golden questions (fast CI gate)")
    ap.add_argument("--no-rerank", action="store_true", help="compare retrieval without rerank")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args(argv)

    cfg = get_config()
    if args.no_rerank:
        cfg.retrieval.rerank_enabled = False

    golden_items = load_yaml(args.golden) if Path(args.golden).exists() else []
    if args.limit:
        golden_items = golden_items[: args.limit]
    red_items = load_yaml(args.redteam) if Path(args.redteam).exists() else []
    if not golden_items and not red_items:
        print("Nothing to run: no golden or red-team file found.")
        return 2

    golden = await run_golden(svc, golden_items)
    red = await run_redteam(svc, red_items)
    report = build_report(cfg, golden, red)

    print(json.dumps({"metrics": report["metrics"], "redteam": report["redteam_metrics"]}, indent=2))
    if not args.no_save:
        s = get_settings()
        path = await save_report(report, s.db_path, Path(s.data_dir) / "eval")
        print(f"Report saved to {path}")

    thresholds = load_yaml(args.thresholds) if Path(args.thresholds).exists() else {}
    failures = gate(report, thresholds)
    for f in failures:
        print("FAIL:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    from app.deps import get_rag

    sys.exit(asyncio.run(run_cli(get_rag(), sys.argv[1:])))
