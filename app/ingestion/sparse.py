class Bm25Embedder:
    """BM25 sparse vectors via fastembed (no neural model, small RAM)."""

    def __init__(self):
        self._model = None

    def _get(self):
        if self._model is None:
            from fastembed import SparseTextEmbedding

            self._model = SparseTextEmbedding("Qdrant/bm25")
        return self._model

    def embed(self, texts: list[str]) -> list[tuple[list[int], list[float]]]:
        return [(e.indices.tolist(), e.values.tolist()) for e in self._get().embed(texts)]

    def embed_query(self, text: str) -> tuple[list[int], list[float]]:
        e = next(iter(self._get().query_embed(text)))
        return e.indices.tolist(), e.values.tolist()
