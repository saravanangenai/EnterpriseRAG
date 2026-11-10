"""In-process caches for the query path.

- `normalize_question` is the key for both the query-embedding cache and the answer cache, so
  "What is the refund policy?" and "what is the refund  policy" share an entry.
- `AnswerCache` is an exact-match answer cache with a TTL and LRU eviction. It lives in this
  process only (fine for one uvicorn worker); move it to Redis before running several workers.

The answer-cache key must change whenever the answer could change, so it includes the corpus
version (bumped on every ingestion and delete), the prompt IDs, the models, the retrieval settings,
the filters, and the caller's access scope.
"""
import hashlib
import json
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

_SPACES = re.compile(r"\s+")


def normalize_question(q: str) -> str:
    """Unicode-normalize, case-fold, collapse whitespace, drop trailing ?, ! and ."""
    q = unicodedata.normalize("NFKC", q).casefold()
    q = _SPACES.sub(" ", q).strip()
    return q.rstrip(" ?!.") or q


def cache_key(**parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


@dataclass
class CachedAnswer:
    answer: str
    hits: list          # the Hit objects the answer cites by number (S1..Sn)
    cited: set[int]
    prompts: dict[str, str]
    created: float = 0.0


@dataclass
class AnswerCache:
    max_entries: int = 256
    ttl_seconds: float = 3600
    clock: Callable[[], float] = time.monotonic
    hits: int = 0
    misses: int = 0
    _data: OrderedDict = field(default_factory=OrderedDict)

    def get(self, key: str) -> CachedAnswer | None:
        entry = self._data.get(key)
        if entry is not None and self.clock() - entry.created > self.ttl_seconds:
            del self._data[key]
            entry = None
        if entry is None:
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return entry

    def put(self, key: str, entry: CachedAnswer) -> None:
        entry.created = self.clock()
        self._data[key] = entry
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
