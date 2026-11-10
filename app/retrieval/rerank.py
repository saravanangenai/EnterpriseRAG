import json
import re

from app.providers.base import LLM
from app.vectorstore import Hit

# The system prompt is prompts/rerank/v<N>.yaml, passed in by the caller.


def parse_order(raw: str, n: int) -> list[int]:
    m = re.search(r"\[[\d,\s]*\]", raw or "")
    order: list[int] = []
    if m:
        try:
            for x in json.loads(m.group(0)):
                if isinstance(x, int) and 0 <= x < n and x not in order:
                    order.append(x)
        except ValueError:
            pass
    return order + [i for i in range(n) if i not in order]  # keep anything the model skipped


async def rerank(llm: LLM, model: str | None, question: str, hits: list[Hit], *, system: str) -> list[Hit]:
    """Listwise rerank with a small LLM. Any failure keeps the original (RRF) order."""
    if len(hits) < 2:
        return hits
    passages = "\n".join(f"[{i}] {h.payload.get('text', '')[:500]}" for i, h in enumerate(hits))
    try:
        raw = await llm.generate(
            system, f"Question: {question}\n\nPassages:\n{passages}", max_tokens=100, model=model
        )
    except Exception:
        return hits
    return [hits[i] for i in parse_order(raw, len(hits))]
