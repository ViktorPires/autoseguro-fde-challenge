import asyncio
import json
import ssl
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agent_service.config import Settings
from agent_service.quote_client import (
    BusinessRefusal,
    Catalogue,
    DependencyError,
    QuoteClient,
    QuoteResult,
)

CATALOGUE = json.loads(
    (Path(__file__).parents[2] / "quote-service/data/plans.json").read_text()
)
INPUTS = {
    "plano_id": "completo",
    "idade": 35,
    "veiculo_ano": 2024,
    "cep": None,
    "data_inicio": None,
}
RESULT = {
    "plano_id": "completo",
    "plano_nome": "Completo",
    "premio_mensal": 209.9,
    "franquia": 3000,
    "coberturas": CATALOGUE["planos"][1]["coberturas"],
    "multiplicadores": {"faixa_etaria": 1, "idade_veiculo": 1, "regiao": 1},
    "carencia": {
        "coberturas": ["roubo", "furto"],
        "dias": 30,
        "observacao": "Carência",
    },
    "moeda": "BRL",
}


class Clock:
    value = 0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([500, 502, 200], 3),
        ([503] * 3, 3),
        ([400], 1),
        ([401], 1),
        ([403], 1),
        ([404], 1),
        ([429], 1),
        ([302], 1),
        ([422], 1),
    ],
)
async def test_exact_attempts(statuses, expected):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            statuses[len(calls) - 1],
            json=RESULT if statuses[len(calls) - 1] == 200 else {},
        )

    clock = Clock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://quote"
    ) as http:
        client = QuoteClient(
            http,
            Settings(hmac_secret="s" * 32),
            clock=clock,
            sleep=clock.sleep,
            jitter=lambda a, b: b,
        )
        if statuses[-1] == 200:
            await client.quote(
                INPUTS,
                Catalogue.model_validate(CATALOGUE),
                client.deadline(),
                str(uuid4()),
            )
        else:
            with pytest.raises(DependencyError):
                await client.quote(
                    INPUTS,
                    Catalogue.model_validate(CATALOGUE),
                    client.deadline(),
                    str(uuid4()),
                )
    assert len(calls) == expected
    assert clock.value <= 0.6 + 1e-9


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError],
)
async def test_transport_retries(error):
    calls = []

    def handler(req):
        calls.append(1)
        raise error("sensitive upstream text")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://quote"
    ) as http:
        client = QuoteClient(
            http, Settings(hmac_secret="s" * 32), jitter=lambda a, b: 0
        )
        with pytest.raises(DependencyError):
            await client.catalogue(client.deadline(), str(uuid4()))
    assert len(calls) == 3


async def test_wall_clock_and_shared_budget():
    calls = []

    async def slow(req):
        calls.append(1)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=CATALOGUE)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(slow), base_url="http://quote"
    ) as http:
        client = QuoteClient(
            http,
            Settings(hmac_secret="s" * 32, attempt_timeout=0.02, quote_deadline=0.05),
            jitter=lambda a, b: 0,
        )
        start = asyncio.get_running_loop().time()
        with pytest.raises(DependencyError):
            await client.catalogue(client.deadline(), str(uuid4()))
        assert asyncio.get_running_loop().time() - start < 0.15
    assert len(calls) == 3


async def test_no_budget_no_attempt():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("unexpected attempt")),
        base_url="http://quote",
    ) as http:
        client = QuoteClient(http, Settings(hmac_secret="s" * 32))
        with pytest.raises(DependencyError):
            await client.catalogue(client.clock(), str(uuid4()))


@pytest.mark.parametrize(
    "payload,exception",
    [
        ({"error": "cotacao_recusada", "motivo": "unknown"}, BusinessRefusal),
        ({"error": "cotacao_recusada", "motivo": 12}, DependencyError),
        ({"detail": [{"loc": ["body", "idade"], "type": "int_type"}]}, DependencyError),
    ],
)
async def test_422_envelopes(payload, exception):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(422, json=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://quote"
    ) as http:
        client = QuoteClient(http, Settings(hmac_secret="s" * 32))
        with pytest.raises(exception):
            await client.quote(
                INPUTS,
                Catalogue.model_validate(CATALOGUE),
                client.deadline(),
                str(uuid4()),
            )
    assert len(calls) == 1


@pytest.mark.parametrize("status", [400, 422])
async def test_lookalike_validation_error_is_contract_failure(status):
    payload = {
        "detail": [
            {
                "loc": ["body", "idade"],
                "type": "provider_database_failure",
                "input": "private sentinel",
            }
        ]
    }

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=payload)
        ),
        base_url="http://quote",
    ) as http:
        client = QuoteClient(http, Settings(hmac_secret="s" * 32))
        with pytest.raises(DependencyError) as exc:
            await client.quote(
                INPUTS,
                Catalogue.model_validate(CATALOGUE),
                client.deadline(),
                str(uuid4()),
            )
    assert exc.value.code == "upstream_contract"
    assert exc.value.field is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("premio_mensal", True),
        ("premio_mensal", -1),
        ("premio_mensal", float("inf")),
        ("premio_mensal", "209.90"),
        ("moeda", "USD"),
        ("coberturas", [1]),
        ("carencia", {"dias": True}),
        ("multiplicadores", {"faixa_etaria": False}),
    ],
)
def test_reject_invalid_success(field, value):
    with pytest.raises(ValueError):
        QuoteResult.model_validate({**RESULT, field: value})


def test_request_consistency():
    cat = Catalogue.model_validate(CATALOGUE)
    result = QuoteResult.model_validate(RESULT)
    result.check_request(INPUTS, cat)
    for inputs in (
        {**INPUTS, "plano_id": "premium"},
        {**INPUTS, "data_inicio": "2026-09-15"},
    ):
        with pytest.raises(ValueError):
            result.check_request(inputs, cat)


async def test_tls_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("private") from ssl.SSLCertVerificationError("private")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://quote"
    ) as http:
        client = QuoteClient(http, Settings(hmac_secret="s" * 32))
        with pytest.raises(DependencyError) as exc:
            await client.catalogue(client.deadline(), str(uuid4()))
        assert exc.value.code == "upstream_tls"
    assert len(calls) == 1


async def test_catalogue_and_quote_share_attempt_budget():
    clock, calls = Clock(), []

    def handler(request):
        calls.append(request)
        clock.value += 1.9
        if request.url.path == "/planos":
            return httpx.Response(200, json=CATALOGUE)
        return httpx.Response(503, json={})

    async with httpx.AsyncClient(
        base_url="http://quote", transport=httpx.MockTransport(handler)
    ) as http:
        client = QuoteClient(
            http,
            Settings(hmac_secret="s" * 32, quote_deadline=3),
            clock=clock,
            sleep=clock.sleep,
            jitter=lambda a, b: 0,
        )
        deadline = client.deadline()
        catalogue = await client.catalogue(deadline, str(uuid4()))
        with pytest.raises(DependencyError):
            await client.quote(INPUTS, catalogue, deadline, str(uuid4()))
    assert len(calls) == 2  # No third attempt once the shared clock budget is gone.


@pytest.mark.parametrize(
    "pro",
    [
        None,
        {"dias_no_mes": 30, "dias_cobrados": 29, "valor_primeiro_pagamento": 10},
        {"dias_no_mes": 30, "dias_cobrados": 16, "valor_primeiro_pagamento": 999},
    ],
)
def test_proration_consistency(pro):
    quote = QuoteResult.model_validate({**RESULT, "primeiro_pagamento_pro_rata": pro})
    with pytest.raises(ValueError):
        quote.check_request(
            {**INPUTS, "data_inicio": "2026-09-15"}, Catalogue.model_validate(CATALOGUE)
        )
