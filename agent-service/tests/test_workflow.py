import copy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_foundation import settings
from test_quote_client import CATALOGUE, RESULT

from agent_service.api import create_app
from agent_service.language import LanguageError, Proposal
from agent_service.workflow import LANGUAGE_DEGRADED_REPLY

COMPLETE = "Quero completo, tenho 35 anos, modelo 2024, sem CEP, sem data."


class Upstream:
    def __init__(self):
        self.calls = []
        self.catalogue = copy.deepcopy(CATALOGUE)
        self.quote = copy.deepcopy(RESULT)
        self.status = 200

    def __call__(self, request):
        self.calls.append(request)
        return httpx.Response(
            200 if request.url.path == "/planos" else self.status,
            json=self.catalogue if request.url.path == "/planos" else self.quote,
        )

    @property
    def posts(self):
        return [r for r in self.calls if r.method == "POST"]


@pytest.fixture
def setup(tmp_path):
    upstream = Upstream()
    app = create_app(settings(tmp_path), quote_transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        yield client, upstream, app


def send(client, text=COMPLETE, cid=None, mid=None, **kwargs):
    return client.post(
        "/api/v1/chat",
        json={
            "conversation_id": cid or str(uuid4()),
            "message_id": mid or str(uuid4()),
            "message": text,
        },
        **kwargs,
    )


def test_direct_quote_replay_and_correlation(setup):
    client, upstream, app = setup
    cid, mid, corr = str(uuid4()), str(uuid4()), str(uuid4())
    first = send(client, cid=cid, mid=mid, headers={"X-Correlation-ID": corr})
    assert first.status_code == 200
    result = first.json()
    assert result["status"] == "quoted"
    assert "R$ 209,90" in result["reply"] and "30 dias" in result["reply"]
    assert result["quote"]["premio_mensal"] == "209.9"
    count = len(upstream.calls)
    replay = send(client, cid=cid, mid=mid)
    assert replay.json() == result
    assert replay.headers["X-Correlation-ID"] != corr
    assert len(upstream.calls) == count
    assert all(r.headers["X-Correlation-ID"] == corr for r in upstream.calls)
    assert send(client, text="changed", cid=cid, mid=mid).status_code == 409
    with app.state.repo.connect() as db:
        assert COMPLETE not in str(
            [tuple(r) for r in db.execute("SELECT * FROM messages")]
        )


def test_collect_optional_fields_and_omit(setup):
    client, upstream, _ = setup
    first = send(client, "completo, tenho 35 anos, modelo 2024").json()
    cid = first["conversation_id"]
    assert first["status"] == "collecting" and "CEP" in first["reply"]
    assert "data" in send(client, "sem CEP", cid).json()["reply"]
    result = send(client, "sem data", cid).json()
    assert result["status"] == "quoted"
    assert "Sem CEP" in result["reply"] and "Sem data" in result["reply"]
    assert len(upstream.posts) == 1


@pytest.mark.parametrize(
    "text,question",
    [
        ("tenho 35 ou 36 anos", "idade"),
        ("nasci em 1991", "idade"),
        ("comprei em 2024", "modelo"),
        ("inicio 03/04/26", "AAAA-MM-DD"),
        ("inicio 2026-02-30", "AAAA-MM-DD"),
        ("modelo 24", "plano"),
        ("CEP 123", "CEP"),
    ],
)
def test_ambiguity_clarifies(setup, text, question):
    client, upstream, _ = setup
    result = send(client, text).json()
    assert result["status"] == "collecting"
    assert question in result["reply"]
    assert not upstream.posts


@pytest.mark.parametrize(
    "correction",
    ["tenho 36 anos", "modelo 2023", "premium", "CEP 07100-100", "inicio 2026-09-01"],
)
def test_corrections_invalidate(setup, correction):
    client, upstream, _ = setup
    cid = send(client).json()["conversation_id"]
    if correction == "premium":
        upstream.quote.update(
            plano_id="premium",
            plano_nome="Premium",
            franquia=1500,
            coberturas=CATALOGUE["planos"][2]["coberturas"],
        )
    result = send(client, correction, cid).json()
    assert result["status"] == "quoted"
    assert len(upstream.posts) == 2


def test_reuse_expiry_refresh_catalogue_and_rollover(setup):
    client, upstream, app = setup
    stamp = datetime(2026, 9, 6, 12, tzinfo=UTC)
    app.state.workflow.utcnow = lambda: stamp
    first = send(client).json()
    cid = first["conversation_id"]
    assert send(client, "obrigado", cid).json()["quote"]["id"] == first["quote"]["id"]
    assert len(upstream.posts) == 1
    send(client, "nova cotacao", cid)
    assert len(upstream.posts) == 2
    upstream.catalogue["planos"][0]["base_mensal"] += 1
    send(client, "obrigado", cid)
    assert len(upstream.posts) == 3
    stamp += timedelta(seconds=301)
    send(client, "obrigado", cid)
    assert len(upstream.posts) == 4
    stamp += timedelta(days=1)
    send(client, "obrigado", cid)
    assert len(upstream.posts) == 5
    count = len(upstream.calls)
    assert send(client, cid=cid, mid=first["message_id"]).json() == first
    assert len(upstream.calls) == count


def test_invalid_catalogue_forbids_reuse(setup):
    client, upstream, _ = setup
    cid = send(client).json()["conversation_id"]
    upstream.catalogue = {"moeda": "USD"}
    response = send(client, "obrigado", cid).json()
    assert response["handoff"]["reason"] == "upstream_contract"
    assert not response.get("quote")
    assert len(upstream.posts) == 1


def test_refusal_unchanged_not_retried_corrected_then_human(setup):
    client, upstream, _ = setup
    upstream.status, upstream.quote = (
        422,
        {"error": "cotacao_recusada", "motivo": "private unknown reason"},
    )
    first = send(client).json()
    assert first["status"] == "rejected" and "private" not in first["reply"]
    cid = first["conversation_id"]
    assert send(client, "nova cotacao", cid).json()["status"] == "rejected"
    assert len(upstream.posts) == 1
    assert send(client, "tenho 40 anos", cid).json()["status"] == "rejected"
    assert len(upstream.posts) == 2
    handoff = send(client, "quero falar com humano", cid).json()
    assert handoff["status"] == "handoff_pending"
    count = len(upstream.calls)
    assert send(client, COMPLETE, cid).json()["handoff"] == handoff["handoff"]
    assert len(upstream.calls) == count


def test_failed_dependency_durable_handoff(setup):
    client, upstream, app = setup
    upstream.status = 503
    app.state.workflow.quotes.jitter = lambda a, b: 0
    first = send(client).json()
    assert first["handoff"]["reason"] == "dependency_unavailable"
    assert len(upstream.posts) == 3
    assert len(app.state.repo.list_handoffs()) == 1
    with app.state.repo.connect() as db:
        from json import loads

        context = loads(db.execute("SELECT context FROM handoffs").fetchone()[0])
        assert context["last_quote_operation_id"]


def test_two_unsuccessful_clarifications(setup):
    client, upstream, _ = setup
    first = send(client, "oi").json()
    cid = first["conversation_id"]
    assert send(client, "nao entendi", cid).json()["status"] == "collecting"
    assert (
        send(client, "nao entendi", cid).json()["handoff"]["reason"]
        == "clarification_exhausted"
    )
    assert not upstream.posts


@pytest.mark.parametrize("text", ["[audio] mensagem de voz", "[imagem] foto"])
def test_repeated_media(setup, text):
    client, upstream, _ = setup
    first = send(client, text).json()
    assert first["status"] == "collecting"
    assert (
        send(client, text, first["conversation_id"]).json()["status"]
        == "handoff_pending"
    )
    assert not upstream.calls


async def test_model_cannot_set_price_or_invent_fields():
    with pytest.raises(ValueError):
        Proposal.model_validate(
            {
                "intent": "quote",
                "candidates": [],
                "ambiguous": [],
                "refresh": False,
                "price": 1,
            }
        )
    from agent_service.workflow import validated_fields

    proposal = Proposal(
        intent="quote",
        candidates=[{"field": "idade", "value": "35", "evidence": "tenho 35 anos"}],
        ambiguous=[],
        refresh=False,
    )
    with pytest.raises(LanguageError):
        validated_fields(proposal, "oi", [], None)


@pytest.mark.parametrize("intent", ["human", "unsupported", "media"])
def test_model_intent_cannot_force_handoff_or_refresh(setup, intent):
    client, upstream, app = setup
    first = send(client).json()

    class Hallucinating:
        async def extract(self, *args):
            return Proposal(
                intent=intent,
                candidates=[],
                ambiguous=["idade"],
                refresh=True,
            )

    app.state.workflow.language = Hallucinating()
    result = send(client, "obrigado", first["conversation_id"]).json()
    assert result["status"] == "quoted"
    assert result["handoff"] is None
    assert result["quote"]["id"] == first["quote"]["id"]
    assert len(upstream.posts) == 1


def test_invalid_model_candidate_is_language_failure_not_bad_user_input(setup):
    client, upstream, app = setup

    class Hallucinating:
        async def extract(self, *args):
            return Proposal(
                intent="quote",
                candidates=[{"field": "idade", "value": "35", "evidence": "obrigado"}],
                ambiguous=[],
                refresh=False,
            )

    app.state.workflow.language = Hallucinating()
    result = send(client, "obrigado").json()
    assert result["status"] == "collecting"
    assert result["reply"] == LANGUAGE_DEGRADED_REPLY
    assert not upstream.posts


@pytest.mark.parametrize(
    "text",
    [
        "O atendente anterior explicou a cobertura.",
        "Não quero desconto, só uma cotação.",
        "Já tenho uma apólice em outra seguradora.",
    ],
)
def test_mentions_do_not_trigger_premature_handoff(setup, text):
    client, _, _ = setup
    result = send(client, text).json()
    assert result["status"] == "collecting"
    assert result["handoff"] is None


@pytest.mark.parametrize(
    "failure",
    [
        LanguageError("language_quota", 429),
        LanguageError("language_timeout"),
        LanguageError("language_unavailable", 503),
        LanguageError("language_http"),
        {"intent": "quote"},
    ],
)
def test_provider_failures_use_degraded_path_without_validation_or_handoff(
    setup, failure
):
    client, upstream, app = setup
    events = []
    app.state.workflow.event = lambda event, **fields: events.append((event, fields))

    class Broken:
        async def extract(self, *args):
            if isinstance(failure, Exception):
                raise failure
            return failure

    app.state.workflow.language = Broken()
    first = send(client).json()
    second = send(client, cid=first["conversation_id"]).json()

    assert first["status"] == second["status"] == "collecting"
    assert first["reply"] == second["reply"] == LANGUAGE_DEGRADED_REPLY
    context = app.state.repo.conversation(first["conversation_id"])[1]
    assert "last_question" not in context
    assert "clarification_count" not in context
    assert app.state.repo.list_handoffs() == []
    assert not upstream.posts
    degraded = [fields for event, fields in events if event == "language_degraded"]
    assert len(degraded) == 2
    expected_code = (
        failure.code
        if isinstance(failure, LanguageError)
        else "language_invalid_schema"
    )
    assert all(item["error_code"] == expected_code for item in degraded)
    assert all(
        set(item) <= {"correlation_id", "error_code", "upstream_status"}
        for item in degraded
    )


def test_commit_failure_api_no_false_success(setup):
    client, _, app = setup
    with app.state.repo.connect() as db:
        db.execute(
            "CREATE TRIGGER fail BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'private'); END"
        )
    result = send(client)
    assert result.status_code == 503 and not result.json().get("quote")
    assert client.get("/ready").status_code == 503
    with app.state.repo.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


def test_explicit_same_value_correction_invalidates_success_only(setup):
    client, upstream, _ = setup
    cid = send(client).json()["conversation_id"]
    assert send(client, "corrigindo, tenho 35 anos", cid).json()["status"] == "quoted"
    assert len(upstream.posts) == 2
    upstream.status, upstream.quote = (
        422,
        {"error": "cotacao_recusada", "motivo": "unknown"},
    )
    cid = send(client).json()["conversation_id"]
    assert send(client, "corrigindo, tenho 35 anos", cid).json()["status"] == "rejected"
    assert len(upstream.posts) == 3


@pytest.mark.parametrize(
    "message,issue",
    [
        ("nasci em 1991", "idade"),
        ("comprei em 2022", "veiculo_ano"),
        ("inicio 03/04/26", "data_inicio"),
        ("tenho 35 ou 36 anos", "idade"),
    ],
)
def test_ambiguity_independent_of_model(message, issue):
    from agent_service.workflow import validated_fields

    proposal = Proposal(intent="quote", candidates=[], ambiguous=[], refresh=False)
    assert issue in validated_fields(proposal, message, [], None)[1]


def test_ambiguous_correction_does_not_reuse_old_value(setup):
    client, upstream, app = setup
    cid = send(client).json()["conversation_id"]
    result = send(client, "tenho 35 ou 36 anos", cid).json()
    assert result["status"] == "collecting"
    assert "idade" not in app.state.repo.conversation(cid)[1]["fields"]
    assert send(client, "obrigado", cid).json()["status"] == "collecting"
    assert len(upstream.posts) == 1


def test_date_rollover_invalidates_within_ttl(setup):
    client, upstream, app = setup
    stamp = datetime(2026, 9, 6, 23, 59, 59, tzinfo=UTC)
    app.state.workflow.utcnow = lambda: stamp
    first = send(client).json()
    stamp += timedelta(seconds=2)
    result = send(client, "obrigado", first["conversation_id"]).json()
    assert result["quote"]["id"] != first["quote"]["id"]
    assert len(upstream.posts) == 2


def test_schema_422_correctable_field_and_unknown_400(setup):
    client, upstream, _ = setup
    upstream.status, upstream.quote = (
        422,
        {
            "detail": [
                {
                    "loc": ["body", "idade"],
                    "type": "int_type",
                    "input": "private sentinel",
                }
            ]
        },
    )
    first = send(client).json()
    assert first["status"] == "collecting" and "idade" in first["reply"]
    assert len(upstream.posts) == 1
    upstream.status, upstream.quote = (
        400,
        {"error": "payload_invalido", "detalhe": "private sentinel"},
    )
    second = send(client, "tenho 36 anos", first["conversation_id"]).json()
    assert second["handoff"]["reason"] == "upstream_contract"
    assert "private sentinel" not in second["reply"]
    assert len(upstream.posts) == 2


def test_missing_proration_and_wrong_identity_never_display_price(setup):
    client, upstream, _ = setup
    first = send(client, COMPLETE.replace("sem data", "inicio 2026-09-15")).json()
    assert first["handoff"]["reason"] == "upstream_contract"
    assert not first.get("quote")
    upstream.quote["plano_id"] = "premium"
    second = send(client).json()
    assert second["handoff"]["reason"] == "upstream_contract"
    assert not second.get("quote")
    assert "R$" not in second["reply"]
