"""Our own model server: the fallback provider.

Talks to any server that implements the OpenAI-compatible
`POST /chat/completions` API (Ollama, vLLM, LM Studio, llama.cpp server),
so the model behind it can change without touching this code. Uses plain
httpx: the server is ours and there's no vendor SDK to speak of.
"""
from __future__ import annotations

import base64
from typing import Sequence

import httpx
from loguru import logger

from app.common.services.llm.base import Completion, ImageInput, ProviderUnavailable


class SelfHostedProvider:
    name = "selfhosted"

    def __init__(
        self,
        base_url: str,
        *,
        vision_model: str,
        text_model: str = "",
        api_key: str = "",
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._vision_model = vision_model
        self._text_model = text_model or vision_model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport  # tests inject a MockTransport

    def is_configured(self) -> bool:
        return bool(self._base_url and self._vision_model)

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 16000,
    ) -> Completion:
        if images:
            user_content: str | list[dict] = [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.media_type};base64,"
                        + base64.standard_b64encode(img.data).decode("ascii")
                    },
                }
                for img in images
            ] + [{"type": "text", "text": prompt}]
            model = self._vision_model
        else:
            user_content = prompt
            model = self._text_model

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            # Low temperature: transcription should be faithful, not creative.
            "temperature": 0.2,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable("timeout", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            # Server down or unreachable; it won't come back mid-request.
            raise ProviderUnavailable("unreachable", sticky=True, detail=str(exc)) from exc

        if resp.status_code != 200:
            logger.warning("Self-hosted LLM returned HTTP {}: {}", resp.status_code, resp.text[:300])
            raise ProviderUnavailable(
                f"http_{resp.status_code}",
                sticky=resp.status_code in (401, 403, 404),
                detail=resp.text[:300],
            )

        try:
            choice = resp.json()["choices"][0]
            text = (choice["message"]["content"] or "").strip()
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailable("bad_response", detail=resp.text[:300]) from exc
        if not text:
            raise ProviderUnavailable("empty_response")
        return Completion(
            text=text, provider=self.name, truncated=choice.get("finish_reason") == "length"
        )
