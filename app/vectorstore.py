"""Vector store interface plus the Qdrant Cloud implementation."""
from dataclasses import dataclass
from typing import Protocol


@dataclass
class Point:
    id: str
    dense: list[float]
    sparse_indices: list[int]
    sparse_values: list[float]
    payload: dict


@dataclass
class Hit:
    id: str
    score: float
    payload: dict


@dataclass
class SearchResult:
    hits: list[Hit]
    top_dense: float  # best cosine score, used by the relevance gate


class VectorStore(Protocol):
    async def search(
        self,
        dense: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        limit: int,
        filters: dict,
    ) -> SearchResult: ...
    async def ensure(self) -> None: ...
    async def existing_ids(self, ids: list[str]) -> set[str]: ...
    async def upsert(self, points: list[Point]) -> None: ...
    async def update_payload(self, point_id: str, payload: dict) -> None: ...
    async def doc_point_ids(self, document_id: str) -> set[str]: ...
    async def delete(self, ids: list[str]) -> None: ...
    async def delete_document(self, document_id: str) -> None: ...


class QdrantStore:
    def __init__(self, url: str, api_key: str, collection: str, dimensions: int):
        from qdrant_client import AsyncQdrantClient

        if not url:
            raise RuntimeError("QDRANT_URL is not set")
        self.client = AsyncQdrantClient(url=url, api_key=api_key or None)
        self.collection = collection
        self.dimensions = dimensions

    async def ensure(self) -> None:
        from qdrant_client import models as m

        c = self.client
        if await c.collection_exists(self.collection):
            info = await c.get_collection(self.collection)
            size = info.config.params.vectors["dense"].size
            if size != self.dimensions:  # embedding guard
                raise RuntimeError(
                    f"Collection {self.collection} has dimension {size}, config says "
                    f"{self.dimensions}. Use a new collection for a new model/dimension."
                )
            return
        await c.create_collection(
            self.collection,
            vectors_config={
                "dense": m.VectorParams(
                    size=self.dimensions, distance=m.Distance.COSINE, on_disk=True
                )
            },
            sparse_vectors_config={"bm25": m.SparseVectorParams(modifier=m.Modifier.IDF)},
            quantization_config=m.ScalarQuantization(
                scalar=m.ScalarQuantizationConfig(type=m.ScalarType.INT8, always_ram=True)
            ),
        )
        for field in ("document_id", "type", "access_group", "file_sha256"):
            await c.create_payload_index(
                self.collection, field_name=field, field_schema=m.PayloadSchemaType.KEYWORD
            )
        await c.create_payload_index(
            self.collection, field_name="page", field_schema=m.PayloadSchemaType.INTEGER
        )

    async def search(
        self,
        dense: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        limit: int,
        filters: dict,
    ) -> SearchResult:
        """Hybrid search: dense + BM25 prefetch, fused server-side with RRF."""
        from qdrant_client import models as m

        conds = [
            m.FieldCondition(key=k, match=m.MatchValue(value=v))
            for k, v in filters.items()
            if v is not None
        ]
        flt = m.Filter(must=conds) if conds else None
        fused = await self.client.query_points(
            self.collection,
            prefetch=[
                m.Prefetch(query=dense, using="dense", limit=limit, filter=flt),
                m.Prefetch(
                    query=m.SparseVector(indices=sparse_indices, values=sparse_values),
                    using="bm25",
                    limit=limit,
                    filter=flt,
                ),
            ],
            query=m.FusionQuery(fusion=m.Fusion.RRF),
            limit=limit,
            with_payload=True,
        )
        top = await self.client.query_points(
            self.collection, query=dense, using="dense", limit=1, query_filter=flt
        )
        top_dense = top.points[0].score if top.points else 0.0
        hits = [Hit(str(p.id), p.score, p.payload or {}) for p in fused.points]
        return SearchResult(hits, top_dense)

    async def existing_ids(self, ids: list[str]) -> set[str]:
        found: set[str] = set()
        for i in range(0, len(ids), 100):
            recs = await self.client.retrieve(
                self.collection, ids=ids[i : i + 100], with_payload=False, with_vectors=False
            )
            found.update(str(r.id) for r in recs)
        return found

    async def upsert(self, points: list[Point]) -> None:
        from qdrant_client import models as m

        structs = [
            m.PointStruct(
                id=p.id,
                vector={
                    "dense": p.dense,
                    "bm25": m.SparseVector(indices=p.sparse_indices, values=p.sparse_values),
                },
                payload=p.payload,
            )
            for p in points
        ]
        for i in range(0, len(structs), 64):
            await self.client.upsert(self.collection, points=structs[i : i + 64], wait=True)

    async def update_payload(self, point_id: str, payload: dict) -> None:
        await self.client.set_payload(self.collection, payload=payload, points=[point_id])

    def _doc_filter(self, document_id: str):
        from qdrant_client import models as m

        return m.Filter(
            must=[m.FieldCondition(key="document_id", match=m.MatchValue(value=document_id))]
        )

    async def doc_point_ids(self, document_id: str) -> set[str]:
        ids: set[str] = set()
        offset = None
        while True:
            recs, offset = await self.client.scroll(
                self.collection,
                scroll_filter=self._doc_filter(document_id),
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            ids.update(str(r.id) for r in recs)
            if offset is None:
                return ids

    async def delete(self, ids: list[str]) -> None:
        from qdrant_client import models as m

        for i in range(0, len(ids), 256):
            await self.client.delete(
                self.collection,
                points_selector=m.PointIdsList(points=ids[i : i + 256]),
                wait=True,
            )

    async def delete_document(self, document_id: str) -> None:
        from qdrant_client import models as m

        await self.client.delete(
            self.collection,
            points_selector=m.FilterSelector(filter=self._doc_filter(document_id)),
            wait=True,
        )
