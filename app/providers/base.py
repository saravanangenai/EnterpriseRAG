from collections.abc import AsyncIterator
from typing import Protocol


class Embedder(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VisionCaptioner(Protocol):
    async def caption(self, image_bytes: bytes, mime: str, prompt: str) -> str: ...


class LLM(Protocol):
    model: str

    async def generate(
        self, system: str, user: str, max_tokens: int = 800, model: str | None = None
    ) -> str: ...

    def stream(self, system: str, user: str, max_tokens: int = 800) -> AsyncIterator[str]: ...
