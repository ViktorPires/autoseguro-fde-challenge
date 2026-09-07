# Execution evidence

`success-dialogue.json` and `failure-dialogue.json` are actual container responses to synthetic demonstration messages. Their corresponding `*-events.jsonl` files contain only the application's correlated, allowlisted operational events. Customer-visible quote details are deliberately separate from operational logs.

Both paths were executed with `scripts/container-smoke.py`, including application restart and exact replay from the persistent SQLite volume. The failure scenario uses the supplied service's documented failure-rate setting of 1 and produces three POST attempts followed by a durable handoff.

`extraction-sample.json` contains 20 de-identified individual dataset lead messages and five explicitly labelled supplemental fixtures. `extraction-evaluation.json` records the offline fake-adapter results. This is a small regression evaluation, not evidence of live-model accuracy. No provider credentials were used in required checks or evidence generation.

Regenerate using the README commands. UUIDs, timestamps and current-date vehicle fixtures will change; original saved replies remain historical results.

## Live production-provider evidence

| Artifact | Contents |
| --- | --- |
| `live-production-config.json` | Sanitized runtime configuration proving production mode with the Gemini provider |
| `live-production-response.json` | Actual successful live Gemini end-to-end quote response |
| `live-production-agent.log` | Actual production container events showing retry recovery, quote completion, and exact replay |