"""Convert a saved report into JSONL for RAGAS (run RAGAS on the dev PC or CI, not the VM).

    python -m evaluation.export_ragas data/eval/eval-<stamp>.json ragas_input.jsonl

Each line: user_input, response, retrieved_contexts, reference (reference only if the golden
item has one). RAGAS needs a judge LLM and its API changes between versions: pin the version
in requirements-dev.txt and follow its docs for the metrics you run (context precision/recall,
faithfulness, answer relevancy). This script does not call RAGAS.
"""
import json
import sys


def main(report_path: str, out_path: str) -> int:
    report = json.load(open(report_path, encoding="utf-8"))
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for r in report["golden"]:
            if not r["answerable"] or not r["contexts"]:
                continue  # RAGAS scores answered questions that had retrieved context
            row = {
                "user_input": r["question"],
                "response": r["answer"],
                "retrieved_contexts": r["contexts"],
            }
            if r.get("reference"):
                row["reference"] = r["reference"]
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    print(f"Wrote {n} rows to {out_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
