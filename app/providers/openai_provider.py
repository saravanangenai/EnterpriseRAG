import base64

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

_retry = retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)


class OpenAIEmbedder:
    def __init__(self, api_key: str, model: str, dimensions: int):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.dimensions = dimensions

    @_retry
    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.embeddings.create(
            model=self.model, input=texts, dimensions=self.dimensions
        )
        return [d.embedding for d in resp.data]


class OpenAIVision:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model

    @_retry
    async def caption(self, image_bytes: bytes, mime: str, prompt: str) -> str:
        b64 = base64.b64encode(image_bytes).decode()
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        )
        return resp.choices[0].message.content or ""
