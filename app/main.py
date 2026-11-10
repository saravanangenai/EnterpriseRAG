from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers import auth as auth_router
from app.routers import chat as chat_router
from app.routers import documents as documents_router
from app.settings import get_config


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_config()  # fail fast on a bad config file
    await init_db()
    yield
    try:  # send any traces still queued before the process exits
        from app.deps import get_tracer

        get_tracer().shutdown()
    except Exception:
        pass


app = FastAPI(title="Enterprise RAG", version="0.1.0", lifespan=lifespan)
app.include_router(auth_router.router)
app.include_router(documents_router.router)
app.include_router(chat_router.router)


@app.get("/health")
async def health():
    cfg = get_config()
    return {
        "status": "ok",
        "llm": f"{cfg.llm.provider}:{cfg.llm.model}",
        "embedding": f"{cfg.embedding.provider}:{cfg.embedding.model}:{cfg.embedding.dimensions}",
        "vision": f"{cfg.vision.provider}:{cfg.vision.model}",
        "collection": cfg.collection_name,
    }


# Serve the built React app (frontend/dist) from the same origin. Mounted last so every
# API route above wins. The UI uses hash routes, so no server-side rewrites are needed.
_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
