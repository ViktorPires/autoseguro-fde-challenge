"""Short SQLite transactions; no network work occurs in this module."""

import hashlib
import hmac
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .schemas import ChatResponse


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Conflict(Exception):
    def __init__(self, code):
        self.code = code


class Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Repository:
    def __init__(self, path, secret):
        self.path, self.secret = path, secret.encode()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.25, factory=Connection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=250")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("unsupported_schema_version")
            if version == 0:
                db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    context TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('processing','completed')),
                    correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, response TEXT,
                    PRIMARY KEY(conversation_id, message_id)
                );
                CREATE UNIQUE INDEX one_active_turn ON messages(conversation_id)
                    WHERE status='processing';
                CREATE TABLE quotes (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                    inputs TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    catalogue_fingerprint TEXT NOT NULL, service_date TEXT NOT NULL,
                    result TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE handoffs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
                    message_id TEXT NOT NULL, reason TEXT NOT NULL,
                    context TEXT NOT NULL, quote_id TEXT REFERENCES quotes(id),
                    created_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
                COMMIT;
                """)
        self.path.chmod(0o600)

    def fingerprint(self, content):
        return hmac.new(
            self.secret, canonical(content).encode(), hashlib.sha256
        ).hexdigest()

    def claim(self, request, correlation_id):
        cid, mid = str(request.conversation_id), str(request.message_id)
        fp = self.fingerprint(request.model_dump(mode="json"))
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND message_id=?",
                (cid, mid),
            ).fetchone()
            if row:
                if not hmac.compare_digest(fp, row["fingerprint"]):
                    raise Conflict("idempotency_conflict")
                if row["status"] == "processing":
                    raise Conflict("message_in_progress")
                return json.loads(row["response"])
            if db.execute(
                "SELECT 1 FROM messages WHERE conversation_id=? AND status='processing'",
                (cid,),
            ).fetchone():
                raise Conflict("conversation_busy")
            stamp = now()
            db.execute(
                "INSERT OR IGNORE INTO conversations VALUES (?, 'collecting', '{}', ?, ?)",
                (cid, stamp, stamp),
            )
            db.execute(
                "INSERT INTO messages VALUES (?, ?, ?, 'processing', ?, ?, ?, NULL)",
                (cid, mid, fp, correlation_id, stamp, stamp),
            )
        return None

    def conversation(self, cid):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (cid,)
            ).fetchone()
            return row["status"], json.loads(row["context"])

    def handoff(self, cid):
        with self.connect() as db:
            row = db.execute(
                "SELECT id, reason FROM handoffs WHERE conversation_id=?", (cid,)
            ).fetchone()
            return dict(row) if row else None

    def finish(self, cid, mid, context, response, quote=None, reason=None):
        # The returned response is safe to expose only after the transaction commits.
        response = dict(response)
        with self.transaction() as db:
            row = db.execute(
                "SELECT status, response FROM messages WHERE conversation_id=? AND message_id=?",
                (cid, mid),
            ).fetchone()
            if row["status"] == "completed":
                return json.loads(row["response"])
            if quote:
                db.execute(
                    "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        quote["id"],
                        cid,
                        canonical(quote["inputs"]),
                        self.fingerprint(quote["inputs"]),
                        quote["catalogue_fingerprint"],
                        quote["service_date"],
                        canonical(quote["result"]),
                        quote["created_at"],
                        quote["expires_at"],
                    ),
                )
            if reason:
                previous_quote = db.execute(
                    "SELECT id FROM quotes WHERE conversation_id=? ORDER BY created_at DESC LIMIT 1",
                    (cid,),
                ).fetchone()
                db.execute(
                    "INSERT OR IGNORE INTO handoffs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        cid,
                        mid,
                        reason,
                        canonical(context),
                        previous_quote["id"] if previous_quote else None,
                        now(),
                    ),
                )
                handoff = db.execute(
                    "SELECT id, reason FROM handoffs WHERE conversation_id=?", (cid,)
                ).fetchone()
                response.update(
                    status="handoff_pending",
                    handoff=dict(handoff),
                    quote=None,
                    reply="Seu caso está pendente de revisão humana. Referência: "
                    + handoff["id"]
                    + ".",
                )
            response = ChatResponse.model_validate(response).model_dump(mode="json")
            stamp = now()
            db.execute(
                "UPDATE conversations SET status=?, context=?, updated_at=? WHERE id=?",
                (response["status"], canonical(context), stamp, cid),
            )
            db.execute(
                "UPDATE messages SET status='completed', response=?, updated_at=? WHERE conversation_id=? AND message_id=?",
                (canonical(response), stamp, cid, mid),
            )
        return response

    def interrupt(self, cid, mid):
        status, context = self.conversation(cid)
        with self.connect() as db:
            row = db.execute(
                "SELECT correlation_id FROM messages WHERE conversation_id=? AND message_id=?",
                (cid, mid),
            ).fetchone()
        return self.finish(
            cid,
            mid,
            context,
            {
                "conversation_id": cid,
                "message_id": mid,
                "correlation_id": row["correlation_id"],
                "status": status,
                "reply": "",
            },
            reason="processing_interrupted",
        )

    def recover(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT conversation_id, message_id FROM messages WHERE status='processing'"
            ).fetchall()
        for row in rows:
            self.interrupt(*row)
        return len(rows)

    def writable(self):
        with self.transaction() as db:
            db.execute("UPDATE conversations SET updated_at=updated_at WHERE 0")

    def reusable(self, cid, inputs, catalogue_fp, service_date, current, active_id):
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM quotes WHERE conversation_id=? AND id=?
                AND fingerprint=? AND catalogue_fingerprint=? AND service_date=?
                AND expires_at>?""",
                (
                    cid,
                    active_id,
                    self.fingerprint(inputs),
                    catalogue_fp,
                    service_date,
                    current,
                ),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            **json.loads(row["result"]),
        }

    def list_handoffs(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, conversation_id, reason, created_at FROM handoffs ORDER BY created_at"
                )
            ]

    def retain(self, days):
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.transaction() as db:
            result = db.execute(
                """DELETE FROM conversations WHERE updated_at<? AND id NOT IN
                (SELECT conversation_id FROM messages WHERE status='processing')""",
                (cutoff,),
            )
            return result.rowcount
