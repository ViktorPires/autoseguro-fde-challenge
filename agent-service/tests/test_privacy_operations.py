import asyncio
import io
import json
import logging
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_foundation import settings
from test_quote_client import CATALOGUE
from test_workflow import COMPLETE, Upstream, send

from agent_service.api import create_app
from agent_service.events import configure_logging
from agent_service.language import (
    GeminiLanguage,
    LanguageError,
    Proposal,
    gemini_proposal_schema,
    sanitize,
)
from agent_service.workflow import LANGUAGE_DEGRADED_REPLY

SENSITIVE = [
    "529.982.247-25",
    "11987654321",
    "secret.person@example.org",
    "ABC1D23",
    "07100-100",
    "Private Sentinel",
]


def test_sanitization():
    raw = "CPF 529.982.247-25, telefone 11987654321, secret.person@example.org, placa ABC1D23, CEP 07100-100, meu nome é Private Sentinel. tenho 35 anos"
    clean, ceps = sanitize(raw)
    assert ceps == ["07100100"]
    assert all(s not in clean for s in SENSITIVE)
    assert "35 anos" in clean


async def test_provider_options_and_no_retries(tmp_path):
    calls = []
    result = Proposal(intent="other", candidates=[], ambiguous=[], refresh=False)

    def handler(req):
        calls.append(req)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": result.model_dump_json()}]},
                    }
                ]
            },
        )

    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = GeminiLanguage(http, cfg)
        assert (
            await adapter.extract("oi", {"cep_status": "present"}, None, []) == result
        )
    request = calls[0]
    payload = json.loads(calls[0].content)
    assert payload["store"] is False
    config = payload["generationConfig"]
    assert config["responseFormat"] == {
        "text": {
            "mimeType": "APPLICATION_JSON",
            "schema": gemini_proposal_schema(),
        }
    }
    assert "responseMimeType" not in config
    assert "responseJsonSchema" not in config
    assert "candidateCount" not in config
    assert "tools" not in payload and "cachedContent" not in payload
    assert len(payload["contents"]) == 1
    assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
    assert request.headers["x-goog-api-key"] == "test-provider-secret"
    assert "test-provider-secret" not in str(request.url)
    assert len(calls) == 1

    calls.clear()

    def broken(req):
        calls.append(req)
        return httpx.Response(500, json={"error": "sensitive provider error"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == "language_unavailable"
    assert exc.value.http_status == 500
    assert len(calls) == 1


@pytest.mark.parametrize(
    "provider_body,expected_code",
    [
        ({}, "language_invalid_response"),
        ({"candidates": []}, "language_invalid_response"),
        (
            {
                "promptFeedback": {"blockReason": "SAFETY"},
                "candidates": [],
            },
            "language_safety",
        ),
        (
            {
                "candidates": [
                    {
                        "finishReason": "SAFETY",
                        "content": {"parts": [{"text": "{}"}]},
                    }
                ]
            },
            "language_safety",
        ),
        (
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "not json"}]},
                    }
                ]
            },
            "language_invalid_json",
        ),
        (
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "intent": "other",
                                            "candidates": [],
                                            "ambiguous": [],
                                            "refresh": False,
                                            "price": 1,
                                        }
                                    )
                                }
                            ]
                        },
                    }
                ]
            },
            "language_invalid_schema",
        ),
    ],
)
async def test_malformed_gemini_output_is_language_error(
    tmp_path, provider_body, expected_code
):
    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=provider_body)
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == expected_code


@pytest.mark.parametrize(
    "status,expected_code",
    [
        (400, "language_http"),
        (401, "language_auth"),
        (403, "language_auth"),
        (404, "language_model"),
        (429, "language_quota"),
        (500, "language_unavailable"),
        (502, "language_unavailable"),
        (503, "language_unavailable"),
        (599, "language_unavailable"),
    ],
)
async def test_safe_http_diagnostic_categories(tmp_path, status, expected_code):
    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status, json={"error": "private raw provider body"}
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == expected_code
    assert exc.value.http_status == status


async def test_timeout_diagnostic_category(tmp_path):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    cfg = settings(
        tmp_path,
        language_provider="gemini",
        provider_secret="test-provider-secret",
        llm_deadline=0.01,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == "language_timeout"


async def test_invalid_provider_json_diagnostic_category(tmp_path):
    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"private non-json provider body")
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == "language_invalid_json"


async def test_transport_failure_diagnostic_category(tmp_path):
    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )

    def disconnected(request):
        raise httpx.ConnectError("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as http:
        with pytest.raises(LanguageError) as exc:
            await GeminiLanguage(http, cfg).extract("oi", {}, None, [])
    assert exc.value.code == "language_http"


def test_logs_provider_input_and_persistence_are_minimized(tmp_path):
    configure_logging()
    stream = io.StringIO()
    logger = logging.getLogger("agent.events")
    logger.handlers = [logging.StreamHandler(stream)]
    requests = []

    def provider(req):
        requests.append(json.loads(req.content))
        return httpx.Response(500, json={"error": " ".join(SENSITIVE)})

    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    app = create_app(
        cfg,
        quote_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=CATALOGUE)
        ),
        provider_transport=httpx.MockTransport(provider),
    )
    with TestClient(app) as client:
        text = "CPF 529.982.247-25, telefone 11987654321, secret.person@example.org, placa ABC1D23, CEP 07100-100, meu nome é Private Sentinel. tenho 35 anos"
        result = send(client, text).json()
        assert result["status"] == "collecting"
        client.post(
            "/api/v1/chat?private=sentinel",
            json={"message": " ".join(SENSITIVE)},
            headers={"X-Correlation-ID": " ".join(SENSITIVE)},
        )
        with app.state.repo.connect() as db:
            records = str(
                [
                    tuple(r)
                    for table in ("conversations", "messages", "quotes", "handoffs")
                    for r in db.execute("SELECT * FROM " + table)
                ]
            )
        assert text not in records
    sent, logs = json.dumps(requests), stream.getvalue()
    assert all(s not in sent and s not in logs and s not in records for s in SENSITIVE)
    assert "test-provider-secret" not in logs
    assert "private=sentinel" not in logs
    assert all("event" in json.loads(line) for line in logs.splitlines())
    provider_events = [
        json.loads(line)
        for line in logs.splitlines()
        if json.loads(line)["event"] == "language_provider_failure"
    ]
    assert provider_events
    assert provider_events[0]["error_code"] == "language_unavailable"
    assert provider_events[0]["upstream_status"] == 500


def test_gemini_failure_uses_degraded_path_without_handoff(tmp_path):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "private provider failure"})

    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    app = create_app(
        cfg,
        quote_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=CATALOGUE)
        ),
        provider_transport=httpx.MockTransport(provider),
    )
    with TestClient(app) as client:
        first = send(client, "quero uma cotação").json()
        assert first["status"] == "collecting"
        assert first["reply"] == LANGUAGE_DEGRADED_REPLY
        second = send(client, "quero uma cotação", first["conversation_id"]).json()
        assert second["status"] == "collecting"
        assert second["reply"] == LANGUAGE_DEGRADED_REPLY
        context = app.state.repo.conversation(first["conversation_id"])[1]
        assert "clarification_count" not in context
        assert app.state.repo.list_handoffs() == []
    assert len(calls) == 2


def test_malformed_gemini_output_uses_degraded_path_without_handoff(tmp_path):
    malformed = {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {"parts": [{"text": '{"intent":"quote"}'}]},
            }
        ]
    }
    cfg = settings(
        tmp_path, language_provider="gemini", provider_secret="test-provider-secret"
    )
    app = create_app(
        cfg,
        quote_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=CATALOGUE)
        ),
        provider_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=malformed)
        ),
    )
    with TestClient(app) as client:
        first = send(client, "quero uma cotação").json()
        assert first["status"] == "collecting"
        assert first["reply"] == LANGUAGE_DEGRADED_REPLY
        assert first["quote"] is None
        second = send(client, "quero uma cotação", first["conversation_id"]).json()
        assert second["status"] == "collecting"
        assert second["reply"] == LANGUAGE_DEGRADED_REPLY
        context = app.state.repo.conversation(first["conversation_id"])[1]
        assert "clarification_count" not in context
        assert app.state.repo.list_handoffs() == []
        assert second["quote"] is None


async def test_overlap_and_cancellation_recover(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def extract(self, *args):
            started.set()
            await release.wait()
            return Proposal(intent="other", candidates=[], ambiguous=[], refresh=False)

    app = create_app(
        settings(tmp_path),
        language=Slow(),
        quote_transport=httpx.MockTransport(Upstream()),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client:
            request = {
                "conversation_id": str(uuid4()),
                "message_id": str(uuid4()),
                "message": COMPLETE,
            }
            first = asyncio.create_task(client.post("/api/v1/chat", json=request))
            await started.wait()
            assert (await client.post("/api/v1/chat", json=request)).json()[
                "error"
            ] == "message_in_progress"
            assert (
                await client.post(
                    "/api/v1/chat", json={**request, "message_id": str(uuid4())}
                )
            ).json()["error"] == "conversation_busy"
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            saved = app.state.repo.claim(
                __import__(
                    "agent_service.schemas", fromlist=["ChatRequest"]
                ).ChatRequest(**request),
                str(uuid4()),
            )
            assert saved["handoff"]["reason"] == "processing_interrupted"


def test_restart_recovers_processing_and_replays_without_network(tmp_path):
    cfg = settings(tmp_path)
    app = create_app(cfg, quote_transport=httpx.MockTransport(Upstream()))
    from agent_service.schemas import ChatRequest

    request = ChatRequest(
        conversation_id=uuid4(), message_id=uuid4(), message="interrupted private text"
    )
    with TestClient(app):
        app.state.repo.claim(request, str(uuid4()))

    def forbidden(req):
        pytest.fail("replay called dependency")

    with TestClient(
        create_app(cfg, quote_transport=httpx.MockTransport(forbidden))
    ) as client:
        result = client.post("/api/v1/chat", json=request.model_dump(mode="json"))
        assert result.json()["handoff"]["reason"] == "processing_interrupted"
        assert client.get("/ready").status_code == 200


async def test_quote_decision_deadline_includes_language_work(tmp_path):
    class Slow:
        async def extract(self, *args):
            await asyncio.sleep(1)
            raise AssertionError("late result must not be used")

    cfg = settings(tmp_path, quote_deadline=0.04)
    app = create_app(
        cfg, language=Slow(), quote_transport=httpx.MockTransport(Upstream())
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client,
    ):
        start = asyncio.get_running_loop().time()
        response = await client.post(
            "/api/v1/chat",
            json={
                "conversation_id": str(uuid4()),
                "message_id": str(uuid4()),
                "message": COMPLETE,
            },
        )
        result = response.json()
        assert result["status"] == "collecting"
        assert result["reply"] == LANGUAGE_DEGRADED_REPLY
        assert result["handoff"] is None
        context = app.state.repo.conversation(result["conversation_id"])[1]
        assert "clarification_count" not in context
        assert asyncio.get_running_loop().time() - start < 0.2


async def test_overall_turn_deadline_durably_finalizes(tmp_path):
    class Slow:
        async def extract(self, *args):
            await asyncio.sleep(10)

    cfg = settings(tmp_path, turn_deadline=3)
    app = create_app(
        cfg, language=Slow(), quote_transport=httpx.MockTransport(Upstream())
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client,
    ):
        start = asyncio.get_running_loop().time()
        response = await client.post(
            "/api/v1/chat",
            json={
                "conversation_id": str(uuid4()),
                "message_id": str(uuid4()),
                "message": COMPLETE,
            },
        )
        assert (
            response.json().get("handoff", {}).get("reason") == "processing_interrupted"
        ), response.json()
        assert asyncio.get_running_loop().time() - start < 3
        assert len(app.state.repo.list_handoffs()) == 1
