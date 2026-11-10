from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderCfg(BaseModel):
    provider: str
    model: str
    rerank_model: str | None = None
    dimensions: int | None = None


class ChunkingCfg(BaseModel):
    size: int = 1000
    overlap_pct: int = 15
    strategy: str = "structure_aware"


class RetrievalCfg(BaseModel):
    top_k: int = 20
    rerank_enabled: bool = True
    rerank_top_n: int = 6
    min_score: float = 0.0


class AuthCfg(BaseModel):
    access_token_minutes: int = 60


class QdrantCfg(BaseModel):
    collection_prefix: str = "erag"


class IngestionCfg(BaseModel):
    max_upload_mb: int = 20
    image_min_px: int = 100
    pdf_render_dpi: int = 120
    caption_concurrency: int = 2
    embed_batch: int = 64
    allowed_ext: list[str] = [
        ".pdf", ".docx", ".xlsx", ".csv", ".html", ".htm", ".md", ".markdown",
        ".txt", ".png", ".jpg", ".jpeg", ".webp",
    ]


class PiiCfg(BaseModel):
    mode: str = "flag"  # off | flag | mask
    language: str = "en"


class AssetsCfg(BaseModel):
    url_ttl_seconds: int = 300


class TracingCfg(BaseModel):
    enabled: bool = True        # still needs LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to be active
    log_content: bool = False   # False: traces carry IDs, scores and counts, not question/answer/document text
    environment: str = "dev"    # tag traces by environment (dev, vm, ci)


class AppConfig(BaseModel):
    tracing: TracingCfg = TracingCfg()
    assets: AssetsCfg = AssetsCfg()
    llm: ProviderCfg
    embedding: ProviderCfg
    vision: ProviderCfg
    ingestion: IngestionCfg = IngestionCfg()
    pii: PiiCfg = PiiCfg()
    chunking: ChunkingCfg = ChunkingCfg()
    retrieval: RetrievalCfg = RetrievalCfg()
    auth: AuthCfg = AuthCfg()
    qdrant: QdrantCfg = QdrantCfg()

    @property
    def collection_name(self) -> str:
        e = self.embedding
        model = e.model.replace("-", "").replace(".", "")
        return f"{self.qdrant.collection_prefix}_{model}_{e.dimensions}"


class Settings(BaseSettings):
    """Secrets and paths, read from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    jwt_secret: str = ""
    openai_api_key: str = ""
    groq_api_key: str = ""
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    data_dir: str = "data"
    config_path: str = "config/config.yaml"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "app.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_config() -> AppConfig:
    raw = yaml.safe_load(Path(get_settings().config_path).read_text(encoding="utf-8"))
    return AppConfig(**raw)
