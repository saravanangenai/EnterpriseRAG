from functools import lru_cache

from app.guardrails import Guard, get_policy
from app.ingestion.pipeline import Ingestor
from app.ingestion.sparse import Bm25Embedder
from app.pii import NullScanner, PresidioScanner
from app.providers import get_embedder, get_llm, get_vision
from app.retrieval.service import RagService
from app.settings import get_config, get_settings
from app.tracing import LangfuseTracer, NullTracer
from app.vectorstore import QdrantStore


@lru_cache
def get_bm25() -> Bm25Embedder:
    return Bm25Embedder()


@lru_cache
def get_pii():
    cfg = get_config()
    return NullScanner() if cfg.pii.mode == "off" else PresidioScanner(cfg.pii.language)


@lru_cache
def get_tracer():
    """Langfuse tracer when enabled and both keys are set, otherwise a no-op."""
    cfg, s = get_config(), get_settings()
    if not (cfg.tracing.enabled and s.langfuse_public_key and s.langfuse_secret_key):
        return NullTracer()
    pii = get_pii()
    mask = None if cfg.pii.mode == "off" else pii.mask  # same Presidio masking as the audit log
    return LangfuseTracer(
        s.langfuse_public_key,
        s.langfuse_secret_key,
        s.langfuse_host,
        mask_fn=mask,
        log_content=cfg.tracing.log_content,
        environment=cfg.tracing.environment,
    )


@lru_cache
def get_store() -> QdrantStore:
    cfg, s = get_config(), get_settings()
    return QdrantStore(s.qdrant_url, s.qdrant_api_key, cfg.collection_name, cfg.embedding.dimensions)


@lru_cache
def get_ingestor() -> Ingestor:
    cfg, s = get_config(), get_settings()
    return Ingestor(
        cfg, s, get_embedder(cfg, s), get_vision(cfg, s), get_store(), get_bm25(), get_pii(),
        get_tracer(),
    )


@lru_cache
def get_rag() -> RagService:
    cfg, s = get_config(), get_settings()
    return RagService(
        cfg, s, get_embedder(cfg, s), get_bm25(), get_store(), get_llm(cfg, s), get_pii(),
        Guard(get_policy()), get_tracer(),
    )
