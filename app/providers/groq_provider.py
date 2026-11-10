from collections.abc import AsyncIterator

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential


class GroqLLM:
    def __init__(self, api_key: str, model: str):
        self.client = AsyncGroq(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=20), reraise=True)
    async def generate(
        self, system: str, user: str, max_tokens: int = 800, model: str | None = None
    ) -> str:
        resp = await self.client.chat.completions.create(
            model=model or self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    async def stream(self, system: str, user: str, max_tokens: int = 800) -> AsyncIterator[str]:
        # No retry once tokens have started flowing; the caller handles a failed stream.
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=max_tokens,
            stream=True,
        )
        async for chunk in resp:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
