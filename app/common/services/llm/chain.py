"""Try providers in order until one answers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence, TypeVar

from loguru import logger

from app.common.services.llm.base import (
    Completion,
    ImageInput,
    LLMProvider,
    ProviderUnavailable,
)

T = TypeVar("T")


@dataclass(frozen=True)
class Attempt:
    provider: str
    reason: str


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        self.attempts = attempts
        if attempts:
            summary = "; ".join(f"{a.provider}: {a.reason}" for a in attempts)
        else:
            summary = "no AI provider is configured"
        super().__init__(summary)


@dataclass
class FallbackChain:
    """An ordered list of providers, for the lifetime of one request.

    Sticky failures are remembered so that, for example, once Claude says
    the credits are gone, the remaining pages go straight to our own model
    instead of each failing against Claude first.

    Provider-agnostic: `run()` takes the call to make on each provider. The
    LLM and speech-to-text chains are thin wrappers over it.
    """

    providers: Sequence[Any]
    _dead: dict[str, str] = field(default_factory=dict)
    # Set once any call was answered by a provider other than the first
    # one that was tried.
    fell_back: bool = False

    def any_configured(self) -> bool:
        return any(p.is_configured() for p in self.providers)

    @property
    def skipped(self) -> dict[str, str]:
        """Providers this chain gave up on, and why."""
        return dict(self._dead)

    async def run(self, call: Callable[[Any], Awaitable[T]]) -> T:
        attempts: list[Attempt] = []
        for provider in self.providers:
            if not provider.is_configured():
                continue
            if provider.name in self._dead:
                attempts.append(Attempt(provider.name, self._dead[provider.name]))
                continue
            try:
                result = await call(provider)
            except ProviderUnavailable as exc:
                logger.warning("Provider {} unavailable: {}", provider.name, exc)
                attempts.append(Attempt(provider.name, exc.reason))
                if exc.sticky:
                    self._dead[provider.name] = exc.reason
                continue
            if attempts:
                self.fell_back = True
            return result
        raise AllProvidersFailed(attempts)


@dataclass
class ProviderChain(FallbackChain):
    """Fallback chain over LLM providers."""

    providers: Sequence[LLMProvider]

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 16000,
    ) -> Completion:
        return await self.run(
            lambda p: p.complete(
                system=system, prompt=prompt, images=images, max_tokens=max_tokens
            )
        )


def build_default_chain() -> ProviderChain:
    """The chain configured by settings (NOTES_PROVIDER_ORDER)."""
    from app.common.services.llm.anthropic_provider import AnthropicProvider
    from app.common.services.llm.selfhosted_provider import SelfHostedProvider
    from app.core.config import settings

    available: dict[str, LLMProvider] = {
        "anthropic": AnthropicProvider(settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL),
        "selfhosted": SelfHostedProvider(
            settings.SELFHOSTED_LLM_BASE_URL,
            vision_model=settings.SELFHOSTED_VISION_MODEL,
            text_model=settings.SELFHOSTED_TEXT_MODEL,
            api_key=settings.SELFHOSTED_LLM_API_KEY,
            timeout=settings.SELFHOSTED_TIMEOUT_SECONDS,
        ),
    }
    order = [name.lower() for name in settings.NOTES_PROVIDER_ORDER if name.lower() in available]
    return ProviderChain([available[name] for name in order])
