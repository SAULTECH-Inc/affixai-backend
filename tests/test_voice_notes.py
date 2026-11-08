"""Voice recordings → lesson notes.

Speech-to-text providers are exercised against mocked HTTP; the pipeline
and route use scripted fakes. No real model is called.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.common.services import lesson_notes, stt
from app.common.services.lesson_notes import NoteMode, NoteSource
from app.common.services.llm import Completion, ProviderChain, ProviderUnavailable
from app.common.services.stt import AudioInput, SpeechChain, SpeechInputError, TranscriptionProvider

AUDIO = AudioInput("lesson.webm", "audio/webm", b"\x1a\x45\xdf\xa3 fake webm")


# ---- Fakes ------------------------------------------------------------------


class FakeSpeech:
    def __init__(self, name: str, script: list, *, configured: bool = True):
        self.name = name
        self._script = list(script)
        self._configured = configured
        self.calls: list[dict] = []

    def is_configured(self):
        return self._configured

    async def transcribe(self, audio, *, language="", prompt=""):
        self.calls.append({"language": language, "prompt": prompt})
        item = self._script.pop(0)
        if isinstance(item, ProviderUnavailable):
            raise item
        return stt.Transcript(text=item, provider=self.name)


class FakeLLM:
    def __init__(self, name: str, script: list, *, configured: bool = True):
        self.name = name
        self._script = list(script)
        self._configured = configured
        self.calls: list[dict] = []

    def is_configured(self):
        return self._configured

    async def complete(self, *, system, prompt, images=(), max_tokens=16000):
        self.calls.append({"system": system, "prompt": prompt})
        item = self._script.pop(0)
        if isinstance(item, ProviderUnavailable):
            raise item
        return Completion(text=item, provider=self.name)


# ---- Transcription provider (mocked HTTP) -------------------------------------


def _provider(handler, **kw) -> TranscriptionProvider:
    defaults = dict(model="gpt-4o-transcribe", api_key="sk-test")
    defaults.update(kw)
    return TranscriptionProvider(
        "cloud", "https://api.example.com/v1/", transport=httpx.MockTransport(handler), **defaults
    )


async def test_transcription_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "  Today we learn photosynthesis.  "})

    result = await _provider(handler).transcribe(AUDIO, language="en", prompt="A teacher.")

    assert (result.text, result.provider) == ("Today we learn photosynthesis.", "cloud")
    assert seen["url"] == "https://api.example.com/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test"
    body = seen["body"]
    for part in (b'name="model"', b"gpt-4o-transcribe", b'name="language"', b'name="prompt"',
                 b"A teacher.", b'filename="lesson.webm"', b"fake webm"):
        assert part in body


async def test_transcription_leaves_out_empty_language_and_prompt():
    seen: dict = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok"})

    await _provider(handler).transcribe(AUDIO)
    assert b'name="language"' not in seen["body"]
    assert b'name="prompt"' not in seen["body"]


@pytest.mark.parametrize(
    "status,payload,reason,sticky",
    [
        (429, {"error": {"code": "insufficient_quota", "message": "quota"}}, "out_of_credits", True),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "rate_limited", True),
        (401, {"error": {"code": "invalid_api_key"}}, "auth_failed", True),
        (404, {"error": {"code": "model_not_found"}}, "model_not_found", True),
        (500, {"error": {"type": "server_error"}}, "http_500", False),
        (400, {"error": {"type": "invalid_request_error"}}, "http_400", False),
    ],
)
async def test_transcription_error_mapping(status, payload, reason, sticky):
    with pytest.raises(ProviderUnavailable) as info:
        await _provider(lambda r: httpx.Response(status, json=payload)).transcribe(AUDIO)
    assert (info.value.reason, info.value.sticky) == (reason, sticky)


async def test_transcription_unreachable_server_is_sticky():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailable) as info:
        await _provider(handler).transcribe(AUDIO)
    assert (info.value.reason, info.value.sticky) == ("unreachable", True)


def test_cloud_needs_a_key_but_selfhosted_does_not():
    assert TranscriptionProvider("cloud", "https://x/v1", model="m").is_configured() is False
    assert (
        TranscriptionProvider("selfhosted", "http://gpu:8000/v1", model="m", requires_key=False)
        .is_configured()
        is True
    )
    assert TranscriptionProvider("selfhosted", "", model="m", requires_key=False).is_configured() is False


async def test_speech_chain_falls_back_when_cloud_quota_runs_out():
    cloud = FakeSpeech("cloud", [ProviderUnavailable("out_of_credits", sticky=True)])
    ours = FakeSpeech("selfhosted", ["heard it"])
    chain = SpeechChain([cloud, ours])

    result = await chain.transcribe(AUDIO, language="yo", prompt="hint")

    assert result.provider == "selfhosted"
    assert ours.calls == [{"language": "yo", "prompt": "hint"}]
    assert chain.fell_back is True


# ---- Validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,ctype",
    [("a.webm", "audio/webm"), ("a.m4a", "audio/mp4"), ("recording", "audio/ogg"), ("a.wav", "")],
)
def test_validate_accepts_recordings(name, ctype):
    stt.validate_audio(AudioInput(name, ctype, b"x"))


@pytest.mark.parametrize(
    "audio,message",
    [
        (AudioInput("notes.pdf", "application/pdf", b"x"), "isn't a supported recording"),
        (AudioInput("a.mp3", "audio/mpeg", b""), "empty"),
        (AudioInput("a.mp3", "audio/mpeg", b"x" * (stt.MAX_AUDIO_BYTES + 1)), "over 25MB"),
    ],
)
def test_validate_rejects(audio, message):
    with pytest.raises(SpeechInputError, match=message):
        stt.validate_audio(audio)


# ---- Pipeline ------------------------------------------------------------------


async def test_transcribe_mode_skips_the_llm():
    llm = FakeLLM("anthropic", [])
    result = await lesson_notes.generate_voice_note(
        SpeechChain([FakeSpeech("cloud", ["Plants make food."])]),
        ProviderChain([llm]),
        AUDIO,
        NoteMode.TRANSCRIBE,
    )
    assert result.note_markdown == "Plants make food."
    assert result.transcript == "Plants make food."
    assert llm.calls == []
    assert result.providers_used == ["speech:cloud"]


async def test_rewrite_uses_the_speech_prompt_and_counts_unsure_words():
    speech = FakeSpeech("cloud", ["em so today, keep quiet, we learn foto synthesis"])
    llm = FakeLLM("anthropic", ["# Photosynthesis\n\nChlorofil[?] absorbs light."])

    result = await lesson_notes.generate_voice_note(
        SpeechChain([speech]),
        ProviderChain([llm]),
        AUDIO,
        NoteMode.REWRITE,
        language="en",
        subject="Biology",
        class_level="SS 1",
    )

    assert speech.calls == [{"language": "en", "prompt": "A Nigerian teacher speaking to a class about Biology."}]
    call = llm.calls[0]
    assert call["system"] == lesson_notes.REWRITE_SYSTEMS[NoteSource.SPEECH]
    assert "foto synthesis" in call["prompt"]
    assert "Class level: SS 1" in call["prompt"]
    assert result.unsure_count == 1
    assert result.providers_used == ["speech:cloud", "anthropic"]


def test_speech_and_handwriting_prompts_differ_where_they_should():
    speech = lesson_notes.REWRITE_SYSTEMS[NoteSource.SPEECH]
    hand = lesson_notes.REWRITE_SYSTEMS[NoteSource.HANDWRITING]
    assert "Pidgin" in speech and "Pidgin" not in hand
    assert "[Diagram" in hand and "[Diagram" not in speech
    # Shared review rules are in both.
    assert "**Check:**" in speech and "**Check:**" in hand


async def test_empty_recording_returns_warning_without_rewrite():
    llm = FakeLLM("anthropic", [])
    result = await lesson_notes.generate_voice_note(
        SpeechChain([FakeSpeech("cloud", [""])]), ProviderChain([llm]), AUDIO, NoteMode.REWRITE
    )
    assert result.note_markdown == ""
    assert llm.calls == []
    assert any("No speech" in w for w in result.warnings)


async def test_backup_warnings_for_each_stage():
    result = await lesson_notes.generate_voice_note(
        SpeechChain(
            [
                FakeSpeech("cloud", [ProviderUnavailable("out_of_credits", sticky=True)]),
                FakeSpeech("selfhosted", ["words"]),
            ]
        ),
        ProviderChain(
            [
                FakeLLM("anthropic", [ProviderUnavailable("out_of_credits", sticky=True)]),
                FakeLLM("selfhosted", ["# Note"]),
            ]
        ),
        AUDIO,
        NoteMode.REWRITE,
    )
    assert any("speech model" in w for w in result.warnings)
    assert any("AI model" in w for w in result.warnings)


# ---- Route ------------------------------------------------------------------------


@pytest.fixture
async def token():
    from app.core import security
    from app.db.models.user import User, UserRole, UserStatus

    u = await User.create(
        email=f"voice-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Voice",
        last_name="Note",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    return security.create_access_token({"sub": str(u.id)})


def _use(monkeypatch, speech: list, llm: list):
    from app.api.routes import notes as notes_route

    monkeypatch.setattr(notes_route.stt, "build_default_speech_chain", lambda: SpeechChain(speech))
    monkeypatch.setattr(notes_route, "build_default_chain", lambda: ProviderChain(llm))


def _post(client, token, data=None, file=("lesson.webm", b"audio-bytes", "audio/webm")):
    return client.post(
        "/api/v1/notes/voice",
        files={"file": file},
        data=data or {},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_voice_route_requires_auth(client):
    r = await client.post("/api/v1/notes/voice", files={"file": ("a.webm", b"x", "audio/webm")})
    assert r.status_code == 401


async def test_voice_route_happy_path(client, token, monkeypatch):
    _use(monkeypatch, [FakeSpeech("cloud", ["raw words"])], [FakeLLM("anthropic", ["# Note"])])

    r = await _post(client, token, {"mode": "rewrite", "language": "HA"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note_markdown"] == "# Note"
    assert body["transcript"] == "raw words"
    assert body["providers_used"] == ["speech:cloud", "anthropic"]


async def test_voice_route_rejects_unknown_language(client, token, monkeypatch):
    _use(monkeypatch, [FakeSpeech("cloud", [])], [FakeLLM("anthropic", [])])
    r = await _post(client, token, {"language": "fr"})
    assert r.status_code == 400


async def test_voice_route_rejects_non_audio(client, token, monkeypatch):
    _use(monkeypatch, [FakeSpeech("cloud", [])], [FakeLLM("anthropic", [])])
    r = await _post(client, token, file=("notes.pdf", b"%PDF", "application/pdf"))
    assert r.status_code == 400
    assert "supported recording" in r.json()["detail"]


async def test_voice_route_transcribe_mode_works_without_an_llm(client, token, monkeypatch):
    _use(
        monkeypatch,
        [FakeSpeech("cloud", ["just the words"])],
        [FakeLLM("anthropic", [], configured=False)],
    )
    r = await _post(client, token, {"mode": "transcribe"})
    assert r.status_code == 200
    assert r.json()["note_markdown"] == "just the words"


async def test_voice_route_503_when_rewrite_needs_an_llm_that_isnt_set_up(client, token, monkeypatch):
    _use(
        monkeypatch,
        [FakeSpeech("cloud", ["words"])],
        [FakeLLM("anthropic", [], configured=False)],
    )
    r = await _post(client, token, {"mode": "rewrite"})
    assert r.status_code == 503


async def test_voice_route_503_when_all_speech_fails(client, token, monkeypatch):
    _use(
        monkeypatch,
        [
            FakeSpeech("cloud", [ProviderUnavailable("out_of_credits", sticky=True)]),
            FakeSpeech("selfhosted", [ProviderUnavailable("unreachable", sticky=True)]),
        ],
        [FakeLLM("anthropic", [])],
    )
    r = await _post(client, token)
    assert r.status_code == 503
    assert "out_of_credits" not in r.json()["detail"]
