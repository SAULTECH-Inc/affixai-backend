"""Claude via the Anthropic API: the primary provider."""
from __future__ import annotations

import base64
from typing import Any, Sequence

from loguru import logger

from app.common.services.llm.base import Completion, ImageInput, ProviderUnavailable


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self, api_key: str, model: str, *, effort: str = "medium", client: Any = None
    ):
        self._api_key = api_key
        self._model = model
        self._effort = effort
        # Tests pass a client wired to a mock transport.
        self._client: Any = client

    def is_configured(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            # Imported lazily so a deploy without the package still boots;
            # the chain just treats this provider as unavailable.
            import anthropic

            # One retry, not the default two: when Claude is struggling we'd
            # rather hand the page to the next provider than make the user
            # wait through three attempts.
            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key, max_retries=1, timeout=180.0
            )
        return self._client

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 16000,
    ) -> Completion:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderUnavailable("not_installed", sticky=True, detail=str(exc)) from exc

        content: list[dict] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.standard_b64encode(img.data).decode("ascii"),
                },
            }
            for img in images
        ]
        content.append({"type": "text", "text": prompt})

        try:
            # Streaming keeps long notes clear of HTTP timeouts. The
            # server-side fallback re-runs a safety-declined request on
            # another Claude model inside the same call.
            async with self._get_client().beta.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"effort": self._effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            ) as stream:
                message = await stream.get_final_message()
        # Most specific first. Credits running out (402), a bad or revoked
        # key, and rate limits will fail the same way for every page, so
        # they're sticky; the chain stops calling Claude for this request.
        except anthropic.AuthenticationError as exc:
            raise ProviderUnavailable("auth_failed", sticky=True, detail=exc.message) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderUnavailable("permission_denied", sticky=True, detail=exc.message) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderUnavailable("rate_limited", sticky=True, detail=exc.message) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 402:
                raise ProviderUnavailable("out_of_credits", sticky=True, detail=exc.message) from exc
            logger.warning("Claude returned HTTP {}: {}", exc.status_code, exc.message)
            raise ProviderUnavailable(
                f"http_{exc.status_code}", sticky=exc.status_code < 500, detail=exc.message
            ) from exc
        except anthropic.APIConnectionError as exc:
            # Includes timeouts (APITimeoutError subclasses this).
            raise ProviderUnavailable("connection_error", detail=str(exc)) from exc

        if message.stop_reason == "refusal":
            raise ProviderUnavailable("refused")

        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if not text:
            raise ProviderUnavailable("empty_response")
        return Completion(
            text=text, provider=self.name, truncated=message.stop_reason == "max_tokens"
        )
