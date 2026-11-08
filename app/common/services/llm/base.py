"""Provider-neutral types for the LLM layer.

Every provider exposes one call, `complete()`: a system prompt, a user
prompt, and optionally some images, in; text out. Anything that stops a
provider from answering is raised as `ProviderUnavailable` so the chain can
move on to the next provider instead of failing the request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class ImageInput:
    """One image to send to a vision model. `data` is the raw encoded bytes
    (JPEG or PNG), not base64; providers encode it the way their API wants."""

    media_type: str
    data: bytes


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    # True when the model hit the output limit and the text is cut off.
    truncated: bool = False


class ProviderUnavailable(Exception):
    """This provider can't answer right now; try the next one.

    `sticky` means the failure will repeat for every call in this request
    (no credentials, credits exhausted, key rejected, rate limited), so the
    chain stops trying this provider for the rest of the request. Transient
    failures (a timeout, a 500, a refusal of one page) aren't sticky.
    """

    def __init__(self, reason: str, *, sticky: bool = False, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.sticky = sticky
        self.detail = detail


class LLMProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        images: Sequence[ImageInput] = (),
        max_tokens: int = 16000,
    ) -> Completion: ...
