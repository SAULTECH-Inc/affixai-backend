"""Speech-to-text with automatic fallback.

Claude doesn't take audio, so transcription has its own providers. Both
speak the OpenAI-compatible `POST /audio/transcriptions` API:

- **cloud** (primary): OpenAI's hosted models by default (gpt-4o-transcribe),
  or any other hosted service with the same API.
- **selfhosted** (fallback): our own Whisper server, e.g. speaches /
  faster-whisper-server or vLLM. This is where a Nigerian-accent fine-tune
  (N-ATLaS, NaijaVox...) gets served once it has been benchmarked.

Same rules as the LLM chain: a provider without credentials is skipped, and
one that runs out of quota, rejects its key, or goes down hands over to the
next.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import httpx
from loguru import logger

from app.common.services.llm import FallbackChain, ProviderUnavailable

# What the transcription APIs accept. Browsers record webm (Chrome,
# Firefox) or mp4/m4a (Safari), both of which are on the list.
AUDIO_EXTENSIONS = {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "oga", "flac"}
# OpenAI's per-file limit. Without ffmpeg (which Vercel can't run) we can't
# split longer recordings, so this is the cap for every provider.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# ISO-639-1 codes the user can pin. Empty = let the model detect it.
# Nigerian Pidgin has no ISO-639-1 code; leave it on auto-detect.
LANGUAGES = {"en", "yo", "ha", "ig"}


@dataclass(frozen=True)
class AudioInput:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class Transcript:
    text: str
    provider: str


class SpeechInputError(ValueError):
    """The upload can't be transcribed (wrong type, empty, too big)."""


def validate_audio(audio: AudioInput) -> None:
    ext = audio.filename.rsplit(".", 1)[-1].lower() if "." in audio.filename else ""
    ctype = (audio.content_type or "").lower()
    if ext not in AUDIO_EXTENSIONS and not ctype.startswith("audio/") and ctype != "video/webm":
        raise SpeechInputError(
            f"{audio.filename} isn't a supported recording. Use MP3, M4A, WAV, WebM, OGG or FLAC."
        )
    if not audio.data:
        raise SpeechInputError("The recording is empty.")
    if len(audio.data) > MAX_AUDIO_BYTES:
        raise SpeechInputError(
            "That recording is over 25MB. Record a shorter clip, or split it into parts."
        )


class TranscriptionProvider:
    """One OpenAI-compatible transcription endpoint."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        model: str,
        api_key: str = "",
        requires_key: bool = True,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._requires_key = requires_key
        self._timeout = timeout
        self._transport = transport  # tests inject a MockTransport

    def is_configured(self) -> bool:
        if not (self._base_url and self._model):
            return False
        return bool(self._api_key) or not self._requires_key

    async def transcribe(self, audio: AudioInput, *, language: str = "", prompt: str = "") -> Transcript:
        data = {"model": self._model, "response_format": "json"}
        if language:
            data["language"] = language
        if prompt:
            # A short hint about the speaker and topic. Helps with accents
            # and subject vocabulary.
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        files = {
            "file": (audio.filename, audio.data, audio.content_type or "application/octet-stream")
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                resp = await client.post(
                    f"{self._base_url}/audio/transcriptions", data=data, files=files, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable("timeout", detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("unreachable", sticky=True, detail=str(exc)) from exc

        if resp.status_code != 200:
            raise _status_error(self.name, resp)

        try:
            text = (resp.json().get("text") or "").strip()
        except (ValueError, AttributeError) as exc:
            raise ProviderUnavailable("bad_response", detail=resp.text[:300]) from exc
        return Transcript(text=text, provider=self.name)


def _status_error(name: str, resp: httpx.Response) -> ProviderUnavailable:
    body = resp.text[:300]
    try:
        error = resp.json().get("error") or {}
        code = error.get("code") or error.get("type") or ""
    except (ValueError, AttributeError):
        code = ""
    status = resp.status_code
    logger.warning("Speech provider {} returned HTTP {}: {}", name, status, body)

    # OpenAI reports exhausted credits as 429 with code insufficient_quota.
    if code == "insufficient_quota" or status == 402:
        return ProviderUnavailable("out_of_credits", sticky=True, detail=body)
    if status == 401:
        return ProviderUnavailable("auth_failed", sticky=True, detail=body)
    if status == 403:
        return ProviderUnavailable("permission_denied", sticky=True, detail=body)
    if status == 404:
        return ProviderUnavailable("model_not_found", sticky=True, detail=body)
    if status == 429:
        return ProviderUnavailable("rate_limited", sticky=True, detail=body)
    return ProviderUnavailable(f"http_{status}", detail=body)


class SpeechChain(FallbackChain):
    """Fallback chain over transcription providers."""

    providers: Sequence[TranscriptionProvider]

    async def transcribe(self, audio: AudioInput, *, language: str = "", prompt: str = "") -> Transcript:
        return await self.run(lambda p: p.transcribe(audio, language=language, prompt=prompt))


def build_default_speech_chain() -> SpeechChain:
    from app.core.config import settings

    available = {
        "cloud": TranscriptionProvider(
            "cloud",
            settings.STT_CLOUD_BASE_URL,
            model=settings.STT_CLOUD_MODEL,
            api_key=settings.STT_CLOUD_API_KEY,
        ),
        "selfhosted": TranscriptionProvider(
            "selfhosted",
            settings.SELFHOSTED_STT_BASE_URL,
            model=settings.SELFHOSTED_STT_MODEL,
            api_key=settings.SELFHOSTED_STT_API_KEY,
            requires_key=False,
            timeout=settings.SELFHOSTED_TIMEOUT_SECONDS,
        ),
    }
    order = [n.lower() for n in settings.STT_PROVIDER_ORDER if n.lower() in available]
    return SpeechChain([available[n] for n in order])
