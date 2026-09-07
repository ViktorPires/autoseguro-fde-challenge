"""Run after Compose build. Uses only fake language; preserves the test volume."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--failure", action="store_true")
    args = parser.parse_args()
    compose = [
        "docker",
        "compose",
        "--env-file",
        "config/development.env.example",
        "-p",
        "agent-acceptance",
        "-f",
        "docker-compose.yml",
        "-f",
        "compose.failure.yml" if args.failure else "compose.test.yml",
    ]

    def run(*parts):
        subprocess.run(
            [*compose, *parts], cwd=ROOT, check=True, stdout=subprocess.DEVNULL
        )

    def get(path):
        with urlopen("http://127.0.0.1:8080" + path, timeout=2) as response:
            return json.load(response)

    def wait():
        for _ in range(100):
            try:
                if get("/ready")["status"] == "ready":
                    return
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(0.2)
        raise RuntimeError("readiness failed")

    try:
        run("up", "-d", "--build")
        wait()
        assert get("/health")["version"] == "1.0.0"
        year = datetime.now(UTC).year - 2
        request = {
            "conversation_id": str(uuid4()),
            "message_id": str(uuid4()),
            "message": f"Quero completo, tenho 35 anos, modelo {year}, sem CEP, sem data.",
        }
        correlation_id = str(uuid4())

        def chat():
            req = Request(
                "http://127.0.0.1:8080/api/v1/chat",
                data=json.dumps(request).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Correlation-ID": correlation_id,
                },
            )
            with urlopen(req, timeout=45) as response:
                assert response.headers["X-Correlation-ID"] == correlation_id
                return json.load(response)

        first = chat()
        assert first["status"] == ("handoff_pending" if args.failure else "quoted"), (
            first["status"]
        )
        if args.failure:
            assert first["handoff"]["reason"] == "dependency_unavailable"
            listing = json.loads(
                subprocess.check_output(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "agent",
                        "python",
                        "-m",
                        "agent_service.cli",
                        "handoffs",
                        "list",
                    ],
                    cwd=ROOT,
                    text=True,
                )
            )
            assert any(row["id"] == first["handoff"]["id"] for row in listing)
            assert all(
                set(row) == {"id", "conversation_id", "reason", "created_at"}
                for row in listing
            )
        run("restart", "agent")
        wait()
        assert chat() == first
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            label = "failure" if args.failure else "success"
            (args.output / f"{label}-dialogue.json").write_text(
                json.dumps(
                    {"request": request, "response": first},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            logs = subprocess.check_output(
                [*compose, "logs", "--no-log-prefix", "agent"], cwd=ROOT, text=True
            )
            events = []
            for line in logs.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("correlation_id") == correlation_id:
                    events.append(event)
            assert events
            (args.output / f"{label}-events.jsonl").write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            )
        print(
            "PASS: health, readiness, "
            + ("retry exhaustion handoff" if args.failure else "quote")
            + ", restart, exact replay"
        )
    finally:
        subprocess.run(
            [*compose, "down"],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()
