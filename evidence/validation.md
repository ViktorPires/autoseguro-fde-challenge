# Local validation results — 2026-09-07

Release `1.0.0` was validated locally with the repository's pinned dependencies and documented commands.

| Check | Result |
|---|---|
| Frozen dependency sync | Passed; 26 packages checked |
| Ruff lint | Passed; 21 Python files checked |
| Pytest suite | Passed; 124 tests, with 2 upstream deprecation warnings |
| Offline evaluation | Passed; 25/25 scenarios using the deterministic fake language adapter |
| Container builds | Passed for the agent and the unchanged `quote-service` |
| Release metadata | Passed; health responses, smoke expectations, runtime events, and OCI image/Compose labels report `1.0.0` |
| Success-path smoke test | Passed, including health, readiness, quoting, restart persistence, and exact replay |
| Failure-path smoke test | Passed, including exhausted quote retries, durable handoff, read-only conversation retrieval, restart persistence, and exact replay |
| Repository checks | Passed; no changes under `quote-service/` |

## Coverage represented by this run

- integration with the real local `quote-service` contract;
- HTTP retry classification, deadlines, and malformed responses;
- SQLite concurrency, recovery, persistence, and idempotent replay;
- Gemini schema handling, privacy boundaries, and separation of provider failures from user-validation failures;
- nested `30s` Gemini, `39s` quote-decision, and `40s` turn deadline defaults and bounds;
- quote reuse and invalidation across conversation updates.

## Environment and limits

- Local checks ran with Python 3.14.6; container validation used the pinned Python 3.12.14 image.
- The two test warnings are deprecations in the FastAPI/Starlette test-client stack; they did not affect the result.
- CI is configured to repeat the checks on Python 3.12, but no remote GitHub Actions run was initiated for this local snapshot.
- No live Gemini credentials or provider-quality evaluation were used. Required tests and the offline evaluation use deterministic fakes.
