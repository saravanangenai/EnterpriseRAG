"""Re-ingest documents from their raw files. Run from the project root:

    python -m scripts.reindex --failed              # retry failed / interrupted ingestions
    python -m scripts.reindex --all                 # rebuild Qdrant (e.g. after a restore or a lost collection)
    python -m scripts.reindex --document-id <id>    # one document
    add --dry-run to list what would be reindexed

On the VM:  cd /opt/erag && sudo -u erag .venv/bin/python -m scripts.reindex --all
It uses the same config, .env, Qdrant collection and providers as the server. Ingestion is memory
heavy: on the 1 GB VM run it at a quiet time. Exit code 1 if any document failed.
"""
import argparse
import asyncio
import json

from app.db import init_db
from app.ingestion.reindex import reindex, select_targets
from app.settings import get_settings


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="newest ready version of every document")
    g.add_argument("--failed", action="store_true", help="documents whose newest version failed")
    g.add_argument("--document-id", help="one document (its newest ready version)")
    ap.add_argument("--dry-run", action="store_true", help="list targets, change nothing")
    args = ap.parse_args()

    s = get_settings()
    await init_db()
    mode = "all" if args.all else "failed" if args.failed else "document"
    targets = await select_targets(s.db_path, mode, args.document_id)
    print(f"{len(targets)} document(s) to {'check' if args.dry_run else 'reindex'}")
    if not targets:
        return 0

    ingestor = None
    if not args.dry_run:
        from app.deps import get_ingestor  # needs provider keys and Qdrant; not needed for a dry run

        ingestor = get_ingestor()
    try:
        results = await reindex(ingestor, s.db_path, s.data_dir, targets, dry_run=args.dry_run)
    finally:
        if ingestor is not None:
            ingestor.tracer.shutdown()  # send queued Langfuse traces before exiting
    for r in results:
        print(json.dumps(r))
    bad = [r for r in results if r["result"] in ("failed", "skipped")]
    print(f"done: {len(results) - len(bad)} ok, {len(bad)} failed or skipped")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
