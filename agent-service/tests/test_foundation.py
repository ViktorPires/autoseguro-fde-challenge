from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_service import __version__
from agent_service.api import create_app
from agent_service.config import Settings


def settings(tmp_path, **kwargs):
    values = {
        "hmac_secret": "development-only-0123456789abcdef",
        "sqlite_path": tmp_path / "db.sqlite3",
        "provider_secret": "",
    }
    values.update(kwargs)
    return Settings(**values)


def test_configuration(tmp_path):
    with pytest.raises(ValidationError):
        Settings(hmac_secret="short")
    with pytest.raises(ValidationError):
        settings(tmp_path, app_env="production")
    with pytest.raises(ValidationError):
        settings(tmp_path, language_provider="gemini")
    with pytest.raises(ValidationError):
        settings(
            tmp_path,
            language_provider="openai",
            provider_secret="provider-key",
        )
    with pytest.raises(ValidationError):
        settings(
            tmp_path,
            language_provider="gemini",
            language_model="other-provider-model",
            provider_secret="provider-key",
        )
    with pytest.raises(ValidationError):
        settings(tmp_path, max_attempts=4)
    with pytest.raises(ValidationError):
        settings(tmp_path, llm_deadline=31)
    with pytest.raises(ValidationError):
        settings(tmp_path, quote_deadline=40)
    with pytest.raises(ValidationError):
        settings(tmp_path, turn_deadline=41)
    production = Settings(
        app_env="production",
        sqlite_path=tmp_path / "production.sqlite3",
        language_provider="gemini",
        provider_secret="gemini-api-key-AbCdEf1234567890",
        hmac_secret="prod-hmac-AbCdEf1234567890-UVWXYZ",
    )
    assert production.language_model == "gemini-3.8-flash"
    assert production.llm_deadline == 30
    assert production.quote_deadline == 39
    assert production.turn_deadline == 40
    assert production.finalization_reserve == 1
    assert production.turn_deadline - production.finalization_reserve == 39
    assert (
        production.quote_deadline - production.max_attempts * production.attempt_timeout
        > production.llm_deadline
    )
    assert __version__ == production.app_version == "1.0.0"


def test_health_and_validation(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "version": "1.0.0",
            "git_sha": "unknown",
        }
        assert client.get("/ready").status_code == 200
        cid = str(uuid4())
        assert (
            client.get("/health", headers={"X-Correlation-ID": cid}).headers[
                "X-Correlation-ID"
            ]
            == cid
        )
        assert client.post("/api/v1/chat", json={"message": "private"}).json() == {
            "error": "invalid_request"
        }
        assert (
            client.post(
                "/api/v1/chat",
                json={
                    "conversation_id": str(uuid4()),
                    "message_id": str(uuid4()),
                    "message": "x" * 4001,
                },
            ).status_code
            == 422
        )
