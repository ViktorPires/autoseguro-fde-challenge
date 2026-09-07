"""Real supplied service, unchanged, with deterministic documented environment knobs."""

import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_foundation import settings
from test_workflow import send

from agent_service.api import create_app


@pytest.fixture(scope="module")
def real_service():
    if url := os.environ.get("REAL_QUOTE_URL"):
        yield url
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "QUOTE_FAILURE_RATE": "0", "QUOTE_SLOW_RATE": "0", "TZ": "UTC"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        cwd=Path(__file__).parents[2] / "quote-service",
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if (
                    httpx.get(url + "/health", timeout=0.2, trust_env=False).status_code
                    == 200
                ):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("real quote service failed to start")
        yield url
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.integration
@pytest.mark.parametrize(
    "age,vehicle_age,cep,start,expected",
    [
        (35, 2, None, None, "quoted"),
        (80, 2, None, None, "rejected"),
        (35, 25, None, None, "rejected"),
        (17, 2, None, None, "rejected"),
        (35, 2, "07100-100", "15", "quoted"),
        (35, 2, "01310-100", "01", "quoted"),
    ],
)
def test_real_business_contract(
    tmp_path, real_service, age, vehicle_age, cep, start, expected
):
    today = datetime.now(UTC).date()
    year = today.year - vehicle_age
    iso = f"{today.year}-{today.month:02d}-{start}" if start else None
    message = (
        f"completo, tenho {age} anos, modelo {year}, "
        + (f"CEP {cep}" if cep else "sem CEP")
        + ", "
        + (f"inicio {iso}" if iso else "sem data")
    )
    with TestClient(create_app(settings(tmp_path, quote_url=real_service))) as client:
        response = send(client, message).json()
    assert response["status"] == expected
    if expected == "quoted":
        inputs = {
            "plano_id": "completo",
            "idade": age,
            "veiculo_ano": year,
            "cep": cep,
            "data_inicio": iso,
        }
        authoritative = httpx.post(
            real_service + "/quote", json=inputs, trust_env=False
        ).json()
        quote = response["quote"]
        assert float(quote["premio_mensal"]) == authoritative["premio_mensal"]
        assert float(quote["franquia"]) == authoritative["franquia"]
        assert quote["carencia"] == authoritative["carencia"]
        assert "30 dias" in response["reply"]
        assert quote["coberturas"] == authoritative["coberturas"]
        if cep == "07100-100":
            assert quote["multiplicadores"]["regiao"] == "1.3"
        if start == "15":
            assert "Primeiro pagamento" in response["reply"]
            assert (
                float(quote["primeiro_pagamento_pro_rata"]["valor_primeiro_pagamento"])
                == authoritative["primeiro_pagamento_pro_rata"][
                    "valor_primeiro_pagamento"
                ]
            )
        else:
            assert quote["primeiro_pagamento_pro_rata"] is None
