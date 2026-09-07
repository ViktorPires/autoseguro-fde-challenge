"""Local demonstration and restricted-volume operational commands."""

import argparse
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from .config import Settings
from .repository import Repository


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat")
    chat.add_argument("--url", default="http://127.0.0.1:8080")
    chat.add_argument("--message", required=True)
    chat.add_argument("--ids", type=Path, default=Path(".chat-ids.json"))
    chat.add_argument("--new-conversation", action="store_true")
    chat.add_argument(
        "--retry",
        action="store_true",
        help="Resend exactly the same message with saved IDs",
    )
    handoffs = sub.add_parser("handoffs")
    handoffs.add_argument("action", choices=["list"])
    sub.add_parser("retention")
    args = parser.parse_args()
    if args.command == "chat":
        ids = (
            json.loads(args.ids.read_text())
            if args.ids.exists() and not args.new_conversation
            else {"conversation_id": str(uuid4())}
        )
        if not args.retry:
            ids["message_id"] = str(uuid4())
        elif "message_id" not in ids:
            parser.error("No saved message ID to retry")
        for value in ids.values():
            UUID(value)
        # Save IDs before network work; never save the user's message.
        fd = os.open(args.ids, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(ids, file)
            file.flush()
            os.fsync(file.fileno())
        try:
            response = httpx.post(
                args.url + "/api/v1/chat",
                json={**ids, "message": args.message},
                timeout=45,
                trust_env=False,
            )
        except httpx.HTTPError:
            print(
                "Request unavailable. Retry the same text with --retry; IDs were saved."
            )
            raise SystemExit(1) from None
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        raise SystemExit(0 if response.is_success else 1)
    cfg = Settings()
    if args.command == "handoffs":
        # URI mode=ro prevents an operator's list command from creating/writing a DB.
        with sqlite3.connect(
            cfg.sqlite_path.resolve().as_uri() + "?mode=ro", uri=True
        ) as db:
            db.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT id, conversation_id, reason, created_at FROM handoffs ORDER BY created_at"
                )
            ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        repo = Repository(cfg.sqlite_path, cfg.hmac_secret.get_secret_value())
        print(json.dumps({"removed_conversations": repo.retain(cfg.retention_days)}))


if __name__ == "__main__":
    main()
