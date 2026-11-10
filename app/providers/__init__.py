"""Provider factory: config chooses the adapter, the pipeline only sees the interfaces."""
from app.providers.base import LLM, Embedder, VisionCaptioner
from app.settings import AppConfig, Settings


def get_embedder(cfg: AppConfig, s: Settings) -> Embedder:
    if cfg.embedding.provider == "openai":
        from app.providers.openai_provider import OpenAIEmbedder

        return OpenAIEmbedder(s.openai_api_key, cfg.embedding.model, cfg.embedding.dimensions)
    raise ValueError(f"Unsupported embedding provider: {cfg.embedding.provider}")


def get_vision(cfg: AppConfig, s: Settings) -> VisionCaptioner:
    if cfg.vision.provider == "openai":
        from app.providers.openai_provider import OpenAIVision

        return OpenAIVision(s.openai_api_key, cfg.vision.model)
    raise ValueError(f"Unsupported vision provider: {cfg.vision.provider}")


def get_llm(cfg: AppConfig, s: Settings) -> LLM:
    if cfg.llm.provider == "groq":
        from app.providers.groq_provider import GroqLLM

        return GroqLLM(s.groq_api_key, cfg.llm.model)
    raise ValueError(f"Unsupported LLM provider: {cfg.llm.provider}")
