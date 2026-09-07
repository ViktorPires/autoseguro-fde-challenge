# Insurance agent: architecture and implementation plan

Status: final design for implementation. This document does not implement the application.

## 1. Scope and deployment

Build one Python/FastAPI application alongside the supplied `quote-service`, with SQLite on a persistent local Docker volume. Run one application instance with one worker. Demonstrate the conversation using a small CLI or curl against the chat endpoint.

Public application routes:

- `POST /api/v1/chat`
- `GET /health`
- `GET /ready`

Handoff visibility consists of a reference in the chat response and a local, read-only `handoffs list` command against SQLite. No handoff HTTP endpoints, assignments, claim/resolve workflow, dashboard, or notification integration.

No Kubernetes, Redis, PostgreSQL, background job system, frontend, WhatsApp integration, multi-agent orchestration, or agent framework. No user accounts or RBAC. The take-home runs on loopback/private networking; internet exposure is outside this deployment contract and requires an access boundary.

```text
CLI / curl
    |
POST /api/v1/chat
    |
Deterministic conversation workflow
    |-- Language parser: structured intent and candidate fields
    |-- Quote client: GET /planos, POST /quote
    `-- SQLite: conversations, messages, quotes, handoffs
```

Use FastAPI/Pydantic for the API and schemas, HTTPX for upstream HTTP, and Python's SQLite driver with a small repository module. Keep API, workflow, language adapter, quote client, persistence, and operational helpers as modules within one application. No generic plugin system, ORM, or dependency-injection framework is needed.

## 2. Deterministic ownership

| Concern | Owner |
| --- | --- |
| Premium, deductible, eligibility, coverage, waiting periods, first-payment proration | Provided quote service |
| Input normalization, clarification, state transitions, retry policy, reuse, handoff | Application code |
| Understanding Portuguese free text and proposing structured intent/fields | LLM adapter |
| Financial and business statements shown to the lead | Deterministic templates populated from validated service results |

Read plan choices and descriptions from `GET /planos`. Every new personalized price must come from `POST /quote`; do not copy the rating algorithm or turn catalogue base prices into personalized estimates. Do not substitute an invented price when dependencies fail.

The service currently calculates without creating a persistent quote or policy. Its quote POST is therefore safe to retry within a bound. This is a property of the provided implementation, not a general assumption about POST endpoints. A production upstream that creates business records would require its own idempotency contract.

The supplied code distinguishes business refusal (`422`, `error=cotacao_recusada`) from FastAPI request validation (`422`, `detail`). It computes vehicle age using its current date, and returns proration only when a supplied start date is not the first of the month. Respect these semantics. Do not promise policy issuance, payment collection, or active coverage: the provided service supports none of them.

## 3. Chat contract and conversation flow

Request:

```json
{
  "conversation_id": "client-generated UUID",
  "message_id": "client-generated UUID",
  "message": "Quero o completo. Tenho 35 anos e meu carro é de 2022."
}
```

Require both IDs, including on the first request. The CLI generates and retains them before sending. The first accepted message creates the conversation; no creation endpoint is needed. Validate UUIDs and bound message length, initially 4,000 characters. IDs must not contain phone numbers, names, or other business identifiers.

The response includes `conversation_id`, `message_id`, `correlation_id`, `status`, `reply`, and optional `quote` and `handoff` objects. Status is one of `collecting`, `quoted`, `rejected`, or `handoff_pending`. A handoff object contains only its ID and safe reason code. A quote includes its local ID, creation timestamp, and validated financial/coverage fields.

Typical flow:

1. Claim the message idempotently before external calls.
2. Load structured conversation context and extract intent/candidate fields.
3. Apply deterministic validation and merge explicit corrections.
4. Ask one focused question if a required field is missing or ambiguous.
5. Otherwise reuse a valid recent quote or call the service immediately.
6. Persist the response and state before replying.

There is no mandatory confirmation state. A complete, unambiguous quote request proceeds directly. Clarification is reserved for missing information, contradictions, ambiguous dates/years, or materially uncertain extraction. Do not silently choose a plan, reinterpret an ambiguous birth year as age, or replace model year with purchase year.

Collect `plano_id`, `idade`, `veiculo_ano`, `cep`, and `data_inicio`. The last two are optional upstream: ask for them when not yet discussed, but accept an explicit omission and explain that the CEP adjustment or first-payment calculation is absent. Track unknown versus explicitly omitted fields separately. Normalize CEP and ISO dates, validate real calendar dates and request bounds, and use plan IDs from the catalogue. The upstream remains the authority on business eligibility.

Render monthly premium, deductible, coverage, waiting periods, and any distinct first payment from the validated response. Corrections to pricing inputs invalidate the active quote. After rejection, corrected inputs may produce a new request; an unchanged rejected request is not automatically retried.

An explicit human request can transition from any state to `handoff_pending`. Once there, further messages return the existing handoff reference without automatic quoting. Resuming automation is outside this take-home's scope.

## 4. SQLite: only the durable state needed

Use four tables, foreign keys, explicit transactions, WAL mode, a bounded busy timeout, and a small schema-version migration mechanism. Keep network calls outside database transactions.

| Table | Minimum contents |
| --- | --- |
| `conversations` | ID, workflow status, normalized fields including explicit omissions, clarification count, timestamps |
| `messages` | Conversation ID + message ID unique key, keyed request fingerprint, processing/completed status, processing correlation ID, timestamps, saved response JSON |
| `quotes` | Local quote-operation ID, conversation ID, normalized input snapshot/fingerprint, catalogue fingerprint, effective service date, validated result JSON, creation/expiry timestamps |
| `handoffs` | ID, unique conversation ID, triggering message ID, reason code, structured context snapshot, last quote-operation ID if any, creation timestamp |

Do not persist raw user transcripts or prompts by default. Structured context, saved templated responses, and necessary quote/handoff data suffice. These records can still contain PII and require restricted volume access and retention.

### Message-level idempotency

- Scope idempotency to `(conversation_id, message_id)`. Fingerprint the canonical request content with HMAC using a stable environment secret; do not log the fingerprint.
- Same key and same content, completed: return the saved business response without calling the LLM or quote service or changing state. This also covers a response lost after a successful database commit.
- Same key with different content: `409 idempotency_conflict`, with no processing.
- Same key still processing: `409 message_in_progress`; instruct the caller to retry the same IDs later. A transient in-progress response is not saved as the final response.
- A different message arriving while the conversation has an active turn receives `409 conversation_busy` before it is accepted. It may be resubmitted unchanged after the active turn finishes.
- Enforce one processing message per conversation with a database constraint and a short atomic claim transaction. No unbounded lock map or distributed locks.
- Commit final conversation state, any quote or handoff, and the saved response atomically. Never report a quote or handoff as recorded if the commit failed.

On application startup, before readiness, recover abandoned processing messages from the previous process into a durable `processing_interrupted` handoff and saved response. The single-instance contract makes these records unambiguously abandoned. Handle runtime deadline expiry/cancellation through the same terminal-state mechanism. If storage fails, return a safe `503` and leave recovery to a later healthy startup; do not claim a handoff exists.

This promises durable local replay and one committed outcome, not exactly-once external execution. A process can die after an upstream calculation and before saving it; the upstream's lack of side effects makes that tolerable.

### Quote reuse is separate from message replay

For a new message, reuse only a successful validated quote from the same conversation with identical normalized pricing inputs, the same validated catalogue fingerprint, and the same effective service date, within an initial five-minute TTL. Explicit corrections invalidate reuse. An explicit request for a fresh quote bypasses reuse. Rejections and technical failures are not successful quote cache entries.

Validate a current `GET /planos` snapshot before authorizing reuse or a new quote; one bounded catalogue read per quote decision is sufficient at this scale. If the catalogue cannot be validated, hand off instead of presenting a reused price as current. Pin both containers to UTC so the date boundary used for reuse agrees with the provided service. The service has no native price-version or expiry contract; the TTL is a conservative application reuse policy, not a guarantee of a binding offer.

Exact message replay remains exact even when its original quote has aged out: the response retains the original creation time and is a historical result. A fresh quote request requires a new message ID.

## 5. Upstream validation and resilience

The quote client owns one reusable HTTP client, connection limits, explicit transport timeouts, response parsing, and all quote-service retries. Disable any additional transport/SDK retry layer.

Release settings:

- At most three attempts per service operation, including the first.
- At most two seconds wall-clock per attempt, with a shorter connect timeout.
- Exponential backoff with full jitter, initially up to 200 ms and then 400 ms.
- One thirty-nine-second monotonic deadline for the entire quote decision: catalogue read, Gemini extraction, quote attempts, and all sleeps share the remaining budget.
- A thirty-second provider-call deadline with no automatic LLM retries, nested inside the quote-decision budget.
- A 40-second overall chat-turn deadline, with one second reserved for durable finalization. The remaining thirty-nine-second workflow window encloses the quote decision. These deadlines run concurrently rather than adding together; the retry attempt count, two-second per-attempt timeout, and backoff policy remain unchanged.

Use an outer wall-clock deadline as well as HTTP phase timeouts: individual socket timeouts do not bound the complete operation. Cap each attempt and sleep to the remaining budget. Do not begin an attempt with no budget remaining. Cancel local work at the deadline and ignore late results; a timed-out server computation may still continue upstream.

| Result | Action |
| --- | --- |
| Transient connection failure/reset or connect/read timeout | Retry within attempt and deadline limits |
| HTTP 5xx | Retry within the same limits |
| Business `422` with validated refusal envelope | No retry; explain refusal and offer human review |
| Schema `422` or input-related `400` | No retry; clarify a known correctable field, otherwise hand off as a contract error |
| Other 4xx, including 401/403/404/429 | No automatic retry; safe technical handoff |
| TLS certificate/configuration error, unexpected redirect | No retry; technical handoff |
| Malformed JSON or invalid successful response | No retry; contract-error handoff; no price displayed |
| Attempts/deadline exhausted | Persist dependency-unavailable handoff |

Validate both `/planos` and `/quote` with explicit Pydantic response schemas. Require expected fields and types; reject booleans as numeric values, non-finite/negative money, invalid currency, and unexpected plan identity. Parse monetary values as decimals for formatting without recomputing prices. Validate coverage lists, multipliers, waiting-period structure, and optional proration fields. Check that proration is present when required by the supplied start date and that its day counts are plausible. Permit harmless additive response fields while strictly validating fields the application uses.

Validate error envelopes before classifying business refusal. Unknown error shapes become technical failures. Never forward raw error bodies or Pydantic errors containing input values to users or operational logs. Map known refusal reasons to approved explanations; use a generic refusal explanation for unrecognized reasons.

## 6. Minimal human handoff

Persist one handoff per conversation for:

- An explicit request for a human.
- Exhausted quote-service retries or deadline.
- Invalid upstream contract or non-correctable integration error.
- Two unsuccessful clarification attempts for the same unresolved issue.
- Unsupported requests requiring business authority, such as discounts or underwriting exceptions.
- Interrupted processing recovered after a restart.

A business refusal is a valid terminal result, not automatically an infrastructure incident. Offer review; create the handoff when requested. An unsupported media marker gets a deterministic request for text/clarification, with handoff after repeated unsupported input or immediately on request. External language-provider failures and invalid provider output use a temporary degraded response without incrementing clarification attempts or creating a handoff. The dataset contains media markers, not transcribable attachments.

Generate handoff context from structured fields and safe reason codes. Atomically save it with the message response before saying the case is pending review. Return an existing reference for repeated handoff requests. Do not promise a notification, assignment, response time, or guaranteed approval.

The local `handoffs list` command shows ID, opaque conversation ID, reason, and creation time by default. It is enough to demonstrate that an operator can find pending work. Detailed context stays in the restricted database; no additional HTTP API or management workflow is required.

## 7. Language model and data handling

Use one configured provider/model behind one narrow adapter. Supply only the current sanitized message, minimal structured context, the last question identifier, and relevant catalogue choices. Ask for schema-constrained intent and candidate fields. Include a small sanitized example set only if evaluation demonstrates value.

Treat model output as untrusted proposals. The model cannot execute tools, write state, calculate financial values, approve exceptions, or choose the retry/handoff policy. Validate extraction and use deterministic response templates. Do not accept a model's self-reported confidence as sufficient evidence to apply ambiguous fields.

Remove unnecessary CPF, phone, email, plate, and other identifiers before provider calls. CEP can be normalized locally and represented by a placeholder plus a present/absent marker; send the provider only what language understanding needs. Do not forward dataset transcripts wholesale or assume redaction catches every identifier. Bound retained context and avoid raw conversation persistence.

Disable provider-side response/conversation storage wherever the selected API supports it, and test that the adapter sets the relevant option. Disable SDK request-body tracing and provider debug logging. Document the selected provider's remaining retention behavior and unsupported controls; a storage-disabled flag must not be described as a guarantee of zero provider retention. Do not select or integrate additional providers for this take-home.

## 8. Traceability and PII-safe logs

Emit structured JSON using an explicit allowlist: UTC timestamp, level, event name, environment, application version, Git SHA, request correlation ID, opaque conversation/message/quote-operation/handoff IDs, old/new workflow status, attempt number, elapsed milliseconds, upstream HTTP status, and safe outcome/error code.

Accept a UUID `X-Correlation-ID` or generate one; never echo arbitrary header contents. Return the current request's ID in the response header and propagate it upstream. Store the original processing correlation ID with the message response. On replay, the body retains that original ID while the header identifies the current HTTP request; log a replay event linking the two. The provided quote service does not itself instrument this header, so application attempt logs are the trace boundary.

Do not log messages, prompts, model output, request/response bodies, quote fields, CEP, age, names, CPF, phone/email, plates, credentials, HMAC fingerprints, or arbitrary exception text. Configure access/error logging to avoid query strings, client IPs, headers, and validation payloads. Log known error categories and safe stack metadata when needed.

Keep SQLite files, `.env` files, provider output, and unsanitized AI exports out of Git. Use separate development/production HMAC secrets and data volumes. Provide a small local retention command, initially thirty days, removing a conversation and dependent records together. Document that idempotency guarantees end when the records expire and that callers must not reuse expired IDs. No scheduler is required for the challenge.

## 9. Configuration, versioning, and health

Validate typed configuration at startup: `APP_ENV=development|production`, quote URL, SQLite path, provider/model and secret, stable HMAC secret, retry/deadline limits, quote TTL, retention period, application version, and Git SHA.

Use one image with separate environment files/secrets and named volumes. Development may use a clearly labelled fake language adapter for offline tests and retain the supplied instability settings. Production disables debug, rejects the fake adapter and placeholder secrets, and uses private network exposure. Do not silently fall back to test behavior.

- `/health`: cheap process liveness, application semantic version, and Git SHA; no external calls or sensitive configuration.
- `/ready`: `200` after configuration validation, schema initialization, interrupted-message recovery, and a bounded database writeability check. Return `503` if durable processing is unavailable.
- Transient quote/LLM outages are degraded operation, so they do not make the application unready. Language-provider failures return a temporary response and do not count as user-validation failures or independently trigger handoff. Do not call the model or `/quote` from health probes. Report dependency failures through normal safe events rather than a new status API.

The public API contract is versioned by `/api/v1`; breaking changes require a new major API path. Application release `1.0.0` and Git SHA identify this release independently. Use a fixed prompt revision and model identifier in deployment metadata for reproducibility. Database schema version is separate and checked before readiness.

## 10. Reproducible build and delivery

Commit an application `pyproject.toml` and `uv.lock`; use frozen dependency installation in local instructions, CI, and Docker. Pin the Python container image by digest and the dependency tooling version. Test dependencies belong in the lockfile too.

The provided quote-service is treated as immutable challenge infrastructure. Do not modify its application code, packaging, Dockerfile, dependency files, or fault simulation. Build and run it as provided.

The agent-service must use locked/frozen dependencies and a reproducible container build independently.

Run containers as non-root, mount SQLite on a writable persistent local volume, keep the upstream internal to Compose for the normal application path, and allow graceful shutdown to finalize or recover active turns. No shared network filesystem or multiple SQLite writers across replicas.

Document single-instance operation, finite replay/reuse windows, provider data handling, and the absence of real human notification. These are explicit scope boundaries, not hidden production guarantees.

## 11. Implementation sequence and acceptance evidence

| Step | Work | Completion evidence |
| --- | --- | --- |
| 1. Foundation | Add application package, typed settings, frozen dependencies, release metadata, three routes, and API schemas | App starts with valid config; invalid config fails clearly; version and health contracts verified |
| 2. Durable core | Add four-table schema, migrations, message claim/finalization, replay/conflict handling, startup recovery, retention helper | Tests prove replay without external work, payload conflict, per-conversation serialization, atomic persistence, and restart recovery |
| 3. Quote boundary | Implement catalogue/quote schemas, retry classification, shared deadline, input snapshots, and reuse | Deterministic tests prove exact attempt counts, elapsed-budget enforcement, schema rejection, and reuse/invalidation |
| 4. Conversation workflow | Implement extraction adapter, sanitization, templates, field merging, clarification, and direct quoting when inputs are clear | Complete input quotes immediately; corrections invalidate results; ambiguous inputs clarify; model output cannot set price or business decisions |
| 5. Handoff and operations | Persist handoffs, add local list command, allowlisted events, storage-disabled provider option, and readiness checks | Repeated requests expose one durable handoff; PII sentinels never appear in logs; provider request options verified |
| 6. Integration and packaging | Compose application with actual quote service; frozen Docker builds; CI | Lint, unit tests, integration tests, image builds, and container smoke checks pass |
| 7. Delivery evidence | README, CLI/scripted conversations, sanitized execution logs, small dataset evaluation, and sanitized `ai-logs/` | Reviewer can run a successful quote and an exhausted-retry handoff from documented commands |

Required automated checks:

- Lint/format checks, unit tests, integration tests, and Docker builds in CI. Required checks use a fake language adapter and never need provider credentials.
- Real-service integration tests with failure/slow rates set to zero: correct quote rendering, age/vehicle refusal, high-risk CEP, waiting periods, optional fields, and mid-month proration. Use fixed business fixtures with a controlled service clock, or dates/vehicle years generated relative to the explicitly configured service date.
- Controlled HTTP fault tests: connection errors, timeout, 5xx then success, exhausted 5xx, invalid success JSON/schema, refusal versus schema `422`, and zero retries for deterministic 4xx. Inject clocks/sleep/jitter in unit tests; use a small HTTP stub for transport/deadline integration tests. Seeded random instability alone does not prove these cases.
- SQLite tests: replay across restart, response lost after commit, duplicate handoff prevention, same ID/different content, overlapping messages, interruption recovery, and commit failure without false success.
- Quote reuse tests: identical inputs within TTL, changed age/year/plan/CEP/start date, catalogue change, date rollover, expiry, explicit refresh, and historical message replay after expiry.
- Privacy tests: distinctive sensitive strings in user text, malformed payloads, provider errors, and upstream errors must not reach operational logs. Confirm provider storage configuration and that persisted records omit raw prompts/transcripts.
- Container smoke test: `/health`, `/ready`, one chat quote with deterministic provider behavior, and persisted state after application restart.

Use a small de-identified dataset sample to evaluate extraction, clarification, unsupported media, and human-request recognition. Historical salesperson messages are examples of language, not trusted pricing or policy facts. Live-model evaluation is an explicit optional local command, separate from required CI. It attempts every case after individual provider failures and reports provider availability separately from extraction quality.

Deliver one successful conversation with correlated operational events and one deterministic failure-to-handoff example. Keep customer-visible sample dialogue separate from PII-safe operational logs. Include sanitized AI-assisted development conversations in `ai-logs/` as required by the challenge; do not fabricate unavailable exports.

## 12. Source basis

- `README.md`: challenge behavior, evaluation criteria, execution evidence, and AI-log delivery requirement.
- `quote-service/app/main.py`: endpoint schemas, stable health endpoint, simulated 5xx/latency, and distinct error envelopes.
- `quote-service/app/quote_logic.py` and `quote-service/data/plans.json`: authoritative pricing, eligibility, waiting periods, date behavior, and proration.
- `dataset/DICIONARIO.md`: synthetic data must still be treated as sensitive; media entries are markers.
- `quote-service/Dockerfile`, `quote-service/pyproject.toml`, and `quote-service/uv.lock`: existing packaging and reproducibility gap.

The user-referenced `RTK.md` was not present in the repository or the local locations searched during architecture analysis; no instructions from that file were assumed.
