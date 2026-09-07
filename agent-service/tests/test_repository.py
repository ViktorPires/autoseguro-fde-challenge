import json
import sqlite3
from uuid import uuid4

import pytest

from agent_service.repository import Conflict, Repository
from agent_service.schemas import ChatRequest


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "db.sqlite3", "test-secret")
    r.initialize()
    return r


def request(cid=None, mid=None, text="private transcript sentinel"):
    return ChatRequest(
        conversation_id=cid or uuid4(), message_id=mid or uuid4(), message=text
    )


def finish(repo, req, **kwargs):
    return repo.finish(
        str(req.conversation_id),
        str(req.message_id),
        {},
        {
            "conversation_id": str(req.conversation_id),
            "message_id": str(req.message_id),
            "correlation_id": str(uuid4()),
            "status": "collecting",
            "reply": "Qual plano?",
        },
        **kwargs,
    )


def test_claim_conflict_busy_and_restart_replay(repo):
    req = request()
    assert repo.claim(req, str(uuid4())) is None
    for candidate, code in [
        (req, "message_in_progress"),
        (request(req.conversation_id), "conversation_busy"),
        (
            request(req.conversation_id, req.message_id, "different"),
            "idempotency_conflict",
        ),
    ]:
        with pytest.raises(Conflict) as exc:
            repo.claim(candidate, str(uuid4()))
        assert exc.value.code == code
    saved = finish(repo, req)
    restarted = Repository(repo.path, "test-secret")
    restarted.initialize()
    assert restarted.recover() == 0
    assert restarted.claim(req, str(uuid4())) == saved  # lost response after commit
    with repo.connect() as db:
        assert req.message not in json.dumps(
            [tuple(r) for r in db.execute("SELECT * FROM messages")]
        )


def test_recovery_and_unique_handoff(repo):
    req = request()
    repo.claim(req, str(uuid4()))
    assert repo.recover() == 1
    first = repo.claim(req, str(uuid4()))
    assert first["handoff"]["reason"] == "processing_interrupted"
    req2 = request(req.conversation_id)
    repo.claim(req2, str(uuid4()))
    assert finish(repo, req2, reason="human_requested")["handoff"] == first["handoff"]
    assert len(repo.list_handoffs()) == 1


def test_atomic_commit_failure(repo):
    req = request()
    repo.claim(req, str(uuid4()))
    with repo.connect() as db:
        db.execute(
            "CREATE TRIGGER fail BEFORE UPDATE ON messages BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        finish(repo, req, reason="human_requested")
    assert repo.list_handoffs() == []
    assert repo.conversation(str(req.conversation_id))[0] == "collecting"


def test_retention_cascades_and_preserves_active(repo):
    done, active = request(), request()
    for req in (done, active):
        repo.claim(req, str(uuid4()))
    finish(repo, done, reason="human_requested")
    with repo.connect() as db:
        db.execute("UPDATE conversations SET updated_at='2000-01-01'")
    assert repo.retain(30) == 1
    assert repo.list_handoffs() == []
    assert repo.conversation(str(active.conversation_id))


def test_independent_connections_race_to_claim(repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    req1 = request()
    req2 = request(req1.conversation_id)
    barrier = Barrier(2)

    def claim(req):
        other = Repository(repo.path, "test-secret")
        barrier.wait()
        try:
            other.claim(req, str(uuid4()))
            return "claimed"
        except Conflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [req1, req2]))
    assert sorted(results) == ["claimed", "conversation_busy"]


def test_future_schema_rejected(repo):
    with repo.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="unsupported_schema_version"):
        repo.initialize()
