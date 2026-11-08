"""Handwritten notes → lesson notes.

Covers the provider fallback chain (the part that has to work when Claude
runs out of credits), page loading, the two-stage pipeline, both providers'
error mapping against mocked HTTP, and the route. No real model is called.
"""
from __future__ import annotations

import io
import json
import uuid
from typing import Sequence

import httpx
import pytest
from PIL import Image

from app.common.services import lesson_notes
from app.common.services.lesson_notes import NoteInputError, NoteMode, UploadedFile
from app.common.services.llm import (
    AllProvidersFailed,
    Completion,
    ImageInput,
    ProviderChain,
    ProviderUnavailable,
)
from app.common.services.llm.anthropic_provider import AnthropicProvider
from app.common.services.llm.selfhosted_provider import SelfHostedProvider


# ---- Helpers ---------------------------------------------------------------


class FakeProvider:
    """Answers from a script: each call pops the next item. A
    ProviderUnavailable item is raised instead of returned."""

    def __init__(self, name: str, script: list, *, configured: bool = True):
        self.name = name
        self._script = list(script)
        self._configured = configured
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        return self._configured

    async def complete(self, *, system, prompt, images: Sequence[ImageInput] = (), max_tokens=16000):
        self.calls.append({"system": system, "prompt": prompt, "images": list(images)})
        item = self._script.pop(0) if self._script else f"{self.name} answer"
        if isinstance(item, ProviderUnavailable):
            raise item
        return Completion(text=item, provider=self.name)


def _png(width=40, height=20, color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _pdf(pages: int) -> bytes:
    import fitz

    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


IMG = ImageInput("image/jpeg", b"x")


# ---- Provider chain --------------------------------------------------------


async def test_chain_uses_first_provider_when_it_works():
    claude = FakeProvider("anthropic", ["from claude"])
    ours = FakeProvider("selfhosted", ["from ours"])
    chain = ProviderChain([claude, ours])

    result = await chain.complete(system="s", prompt="p")

    assert (result.text, result.provider) == ("from claude", "anthropic")
    assert ours.calls == []
    assert chain.fell_back is False


async def test_chain_falls_back_when_credits_run_out_and_stops_retrying_claude():
    claude = FakeProvider(
        "anthropic", [ProviderUnavailable("out_of_credits", sticky=True)]
    )
    ours = FakeProvider("selfhosted", ["page 1", "page 2"])
    chain = ProviderChain([claude, ours])

    first = await chain.complete(system="s", prompt="p1")
    second = await chain.complete(system="s", prompt="p2")

    assert [first.provider, second.provider] == ["selfhosted", "selfhosted"]
    # Claude was asked once; after a sticky failure it's skipped.
    assert len(claude.calls) == 1
    assert chain.fell_back is True
    assert chain.skipped == {"anthropic": "out_of_credits"}


async def test_chain_retries_claude_after_a_transient_failure():
    claude = FakeProvider("anthropic", [ProviderUnavailable("connection_error"), "back"])
    ours = FakeProvider("selfhosted", ["ours"])
    chain = ProviderChain([claude, ours])

    assert (await chain.complete(system="s", prompt="p")).provider == "selfhosted"
    assert (await chain.complete(system="s", prompt="p")).provider == "anthropic"


async def test_chain_skips_unconfigured_provider_without_counting_a_fallback():
    claude = FakeProvider("anthropic", [], configured=False)
    ours = FakeProvider("selfhosted", ["ours"])
    chain = ProviderChain([claude, ours])

    result = await chain.complete(system="s", prompt="p")

    assert result.provider == "selfhosted"
    assert claude.calls == []
    assert chain.fell_back is False


async def test_chain_raises_with_every_reason_when_all_fail():
    chain = ProviderChain(
        [
            FakeProvider("anthropic", [ProviderUnavailable("out_of_credits", sticky=True)]),
            FakeProvider("selfhosted", [ProviderUnavailable("unreachable", sticky=True)]),
        ]
    )
    with pytest.raises(AllProvidersFailed) as info:
        await chain.complete(system="s", prompt="p")
    assert [(a.provider, a.reason) for a in info.value.attempts] == [
        ("anthropic", "out_of_credits"),
        ("selfhosted", "unreachable"),
    ]


def test_chain_reports_nothing_configured():
    chain = ProviderChain([FakeProvider("anthropic", [], configured=False)])
    assert chain.any_configured() is False


# ---- Page loading ----------------------------------------------------------


def test_load_pages_shrinks_photos_and_reencodes_as_jpeg():
    pages = lesson_notes.load_pages(
        [UploadedFile("board.png", "image/png", _png(4000, 2000))], max_pages=10
    )
    assert len(pages) == 1
    assert pages[0].media_type == "image/jpeg"
    img = Image.open(io.BytesIO(pages[0].data))
    assert max(img.size) == lesson_notes.MAX_IMAGE_EDGE


def test_load_pages_applies_exif_rotation():
    # A 40x20 image flagged "rotate 90°" must come out portrait.
    img = Image.new("RGB", (40, 20), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())

    pages = lesson_notes.load_pages([UploadedFile("p.jpg", "image/jpeg", buf.getvalue())], 10)

    assert Image.open(io.BytesIO(pages[0].data)).size == (20, 40)


def test_load_pages_splits_pdf_into_pages_in_upload_order():
    pages = lesson_notes.load_pages(
        [
            UploadedFile("first.png", "image/png", _png()),
            UploadedFile("rest.pdf", "application/pdf", _pdf(2)),
        ],
        max_pages=10,
    )
    assert len(pages) == 3


def test_load_pages_rejects_too_many_pages():
    with pytest.raises(NoteInputError, match="more than 2 pages"):
        lesson_notes.load_pages([UploadedFile("a.pdf", "application/pdf", _pdf(3))], 2)


@pytest.mark.parametrize(
    "name,ctype,message",
    [
        ("photo.heic", "image/heic", "HEIC"),
        ("notes.docx", "application/octet-stream", "isn't supported"),
    ],
)
def test_load_pages_rejects_unsupported_files(name, ctype, message):
    with pytest.raises(NoteInputError, match=message):
        lesson_notes.load_pages([UploadedFile(name, ctype, b"data")], 10)


def test_load_pages_rejects_corrupt_image():
    with pytest.raises(NoteInputError, match="couldn't be read"):
        lesson_notes.load_pages([UploadedFile("p.jpg", "image/jpeg", b"not an image")], 10)


# ---- Pipeline ----------------------------------------------------------------


async def test_transcribe_mode_returns_transcript_without_a_rewrite_call():
    provider = FakeProvider("anthropic", ["Photosynthesis", "Respiration"])
    result = await lesson_notes.generate_note(
        ProviderChain([provider]), [IMG, IMG], NoteMode.TRANSCRIBE
    )

    assert len(provider.calls) == 2  # one per page, nothing else
    assert all(c["system"] == lesson_notes.TRANSCRIBE_SYSTEM for c in provider.calls)
    assert "*Page 1*\n\nPhotosynthesis" in result.note_markdown
    assert "*Page 2*\n\nRespiration" in result.note_markdown
    assert result.providers_used == ["anthropic"]


async def test_rewrite_mode_sends_every_page_and_the_context():
    provider = FakeProvider(
        "anthropic", ["Plants make food", "using light", "# Photosynthesis\n\nNote [illegible]"]
    )
    result = await lesson_notes.generate_note(
        ProviderChain([provider]),
        [IMG, IMG],
        NoteMode.EXPAND,
        subject="Biology",
        class_level="JSS 2",
    )

    rewrite_call = provider.calls[-1]
    assert rewrite_call["system"] == lesson_notes.REWRITE_SYSTEMS[lesson_notes.NoteSource.HANDWRITING]
    assert rewrite_call["images"] == []
    assert "Subject: Biology" in rewrite_call["prompt"]
    assert "Class level: JSS 2" in rewrite_call["prompt"]
    assert "Plants make food" in rewrite_call["prompt"]
    assert "using light" in rewrite_call["prompt"]
    assert "*(added)*" in rewrite_call["prompt"]  # the expand-mode instructions
    assert result.note_markdown.startswith("# Photosynthesis")
    assert result.illegible_count == 1
    assert [p.text for p in result.pages] == ["Plants make food", "using light"]


async def test_blank_pages_are_left_out_of_the_note_and_reported():
    provider = FakeProvider("anthropic", ["[blank page]", "Real notes", "# Note"])
    result = await lesson_notes.generate_note(
        ProviderChain([provider]), [IMG, IMG], NoteMode.REWRITE
    )
    assert "[blank page]" not in provider.calls[-1]["prompt"]
    assert any("page(s) 1" in w for w in result.warnings)


async def test_all_blank_pages_skip_the_rewrite():
    provider = FakeProvider("anthropic", ["[blank page]"])
    result = await lesson_notes.generate_note(ProviderChain([provider]), [IMG], NoteMode.REWRITE)
    assert len(provider.calls) == 1
    assert result.note_markdown == ""


async def test_pipeline_warns_when_backup_model_was_used():
    claude = FakeProvider("anthropic", [ProviderUnavailable("out_of_credits", sticky=True)])
    ours = FakeProvider("selfhosted", ["page", "# Note"])
    result = await lesson_notes.generate_note(
        ProviderChain([claude, ours]), [IMG], NoteMode.REWRITE
    )
    assert result.providers_used == ["selfhosted"]
    assert any("backup model" in w for w in result.warnings)


# ---- Anthropic provider (mocked HTTP) ----------------------------------------


def _anthropic_with(handler) -> AnthropicProvider:
    import anthropic
    import httpx2

    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider("test-key", "claude-opus-5-5", client=client)


def _sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message_stream(text: str, stop_reason: str = "end_turn") -> bytes:
    return _sse(
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    )


async def test_anthropic_provider_sends_image_and_fallback_options():
    import httpx2

    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(
            200, content=_message_stream("Transcribed"), headers={"content-type": "text/event-stream"}
        )

    result = await _anthropic_with(handler).complete(
        system="sys", prompt="Transcribe page 1.", images=[ImageInput("image/jpeg", b"\xff\xd8")]
    )

    assert (result.text, result.provider, result.truncated) == ("Transcribed", "anthropic", False)
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert content[-1] == {"type": "text", "text": "Transcribe page 1."}


async def test_anthropic_provider_flags_truncated_output():
    import httpx2

    provider = _anthropic_with(
        lambda r: httpx2.Response(
            200,
            content=_message_stream("partial", "max_tokens"),
            headers={"content-type": "text/event-stream"},
        )
    )
    assert (await provider.complete(system="s", prompt="p")).truncated is True


async def test_anthropic_provider_treats_refusal_as_unavailable():
    import httpx2

    provider = _anthropic_with(
        lambda r: httpx2.Response(
            200, content=_message_stream("", "refusal"), headers={"content-type": "text/event-stream"}
        )
    )
    with pytest.raises(ProviderUnavailable) as info:
        await provider.complete(system="s", prompt="p")
    assert info.value.reason == "refused"
    assert info.value.sticky is False


@pytest.mark.parametrize(
    "status,error_type,reason,sticky",
    [
        (402, "billing_error", "out_of_credits", True),
        (401, "authentication_error", "auth_failed", True),
        (403, "permission_error", "permission_denied", True),
        (429, "rate_limit_error", "rate_limited", True),
        (529, "overloaded_error", "http_529", False),
        (500, "api_error", "http_500", False),
    ],
)
async def test_anthropic_provider_maps_errors(status, error_type, reason, sticky):
    import httpx2

    def handler(request):
        return httpx2.Response(
            status, json={"type": "error", "error": {"type": error_type, "message": "nope"}}
        )

    with pytest.raises(ProviderUnavailable) as info:
        await _anthropic_with(handler).complete(system="s", prompt="p")
    assert (info.value.reason, info.value.sticky) == (reason, sticky)


def test_anthropic_provider_needs_a_key():
    assert AnthropicProvider("", "claude-opus-5-5").is_configured() is False
    assert AnthropicProvider("sk-ant-x", "claude-opus-5-5").is_configured() is True


# ---- Self-hosted provider (mocked HTTP) --------------------------------------


def _selfhosted_with(handler, **kw) -> SelfHostedProvider:
    return SelfHostedProvider(
        "http://gpu-box:11434/v1/",
        vision_model="qwen2.5vl:7b",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def _chat_ok(text: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    )


async def test_selfhosted_sends_images_to_vision_model():
    seen: dict = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return _chat_ok("page text")

    result = await _selfhosted_with(handler, api_key="secret").complete(
        system="sys", prompt="Transcribe page 1.", images=[ImageInput("image/jpeg", b"abc")]
    )

    assert (result.text, result.provider) == ("page text", "selfhosted")
    assert seen["url"] == "http://gpu-box:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    body = seen["body"]
    assert body["model"] == "qwen2.5vl:7b"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    user = body["messages"][1]["content"]
    assert user[0]["image_url"]["url"] == "data:image/jpeg;base64,YWJj"
    assert user[-1] == {"type": "text", "text": "Transcribe page 1."}


async def test_selfhosted_uses_text_model_for_text_only_calls():
    seen: dict = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return _chat_ok("# Note")

    await _selfhosted_with(handler, text_model="llama3.1:8b").complete(system="s", prompt="p")

    assert seen["body"]["model"] == "llama3.1:8b"
    assert seen["body"]["messages"][1]["content"] == "p"


async def test_selfhosted_flags_truncation():
    result = await _selfhosted_with(lambda r: _chat_ok("part", "length")).complete(
        system="s", prompt="p"
    )
    assert result.truncated is True


async def test_selfhosted_unreachable_is_sticky():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderUnavailable) as info:
        await _selfhosted_with(handler).complete(system="s", prompt="p")
    assert (info.value.reason, info.value.sticky) == ("unreachable", True)


async def test_selfhosted_bad_payload_is_unavailable():
    with pytest.raises(ProviderUnavailable) as info:
        await _selfhosted_with(lambda r: httpx.Response(200, json={"oops": 1})).complete(
            system="s", prompt="p"
        )
    assert info.value.reason == "bad_response"


def test_selfhosted_needs_a_url():
    assert SelfHostedProvider("", vision_model="m").is_configured() is False


# ---- Route -------------------------------------------------------------------


@pytest.fixture
async def token():
    from app.core import security
    from app.db.models.user import User, UserRole, UserStatus

    u = await User.create(
        email=f"notes-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="dummy",
        first_name="Note",
        last_name="Taker",
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
    )
    # New users get a trial subscription, which passes
    # require_active_subscription.
    return security.create_access_token({"sub": str(u.id)})


def _use_chain(monkeypatch, *providers):
    from app.api.routes import notes as notes_route

    monkeypatch.setattr(notes_route, "build_default_chain", lambda: ProviderChain(list(providers)))


def _upload():
    return {"files": ("page1.png", _png(), "image/png")}


async def test_route_requires_auth(client):
    r = await client.post("/api/v1/notes/handwritten", files=_upload())
    assert r.status_code == 401


async def test_route_returns_note_and_transcript(client, token, monkeypatch):
    _use_chain(monkeypatch, FakeProvider("anthropic", ["raw page", "# Clean note"]))

    r = await client.post(
        "/api/v1/notes/handwritten",
        files=_upload(),
        data={"mode": "rewrite", "subject": "Maths"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "rewrite"
    assert body["note_markdown"] == "# Clean note"
    assert body["pages"] == [{"page": 1, "text": "raw page", "provider": "anthropic"}]
    assert body["providers_used"] == ["anthropic"]


async def test_route_rejects_unknown_mode(client, token, monkeypatch):
    _use_chain(monkeypatch, FakeProvider("anthropic", []))
    r = await client.post(
        "/api/v1/notes/handwritten",
        files=_upload(),
        data={"mode": "poem"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


async def test_route_rejects_unsupported_file(client, token, monkeypatch):
    _use_chain(monkeypatch, FakeProvider("anthropic", []))
    r = await client.post(
        "/api/v1/notes/handwritten",
        files={"files": ("notes.docx", b"x", "application/octet-stream")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "isn't supported" in r.json()["detail"]


async def test_route_503_when_nothing_configured(client, token, monkeypatch):
    _use_chain(monkeypatch, FakeProvider("anthropic", [], configured=False))
    r = await client.post(
        "/api/v1/notes/handwritten", files=_upload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 503
    assert "aren't set up" in r.json()["detail"]


async def test_route_503_when_every_provider_fails_without_leaking_reasons(
    client, token, monkeypatch
):
    _use_chain(
        monkeypatch,
        FakeProvider("anthropic", [ProviderUnavailable("out_of_credits", sticky=True)]),
        FakeProvider("selfhosted", [ProviderUnavailable("unreachable", sticky=True)]),
    )
    r = await client.post(
        "/api/v1/notes/handwritten", files=_upload(), headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 503
    assert "out_of_credits" not in r.json()["detail"]
