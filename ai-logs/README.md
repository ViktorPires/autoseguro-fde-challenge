# AI development logs

This directory contains sanitized conversation exports from the AI-assisted development of the AutoSeguro FDE take-home.

The files are derived from native Codex JSONL session exports. They preserve the available user and assistant messages and their original timestamps, while removing non-conversational internal metadata and sensitive information.

## Included sessions

| File | Coverage |
| --- | --- |
| `01-planning.jsonl` | Architecture analysis and refinement, including the plan written to `PLANNING.md`. |
| `02-implementation.jsonl` | Core implementation, Gemini integration/debugging, provider reliability work, and local/CI validation fixes. |
| `03-review-release.jsonl` | Production review, provider-vs-user failure separation, final documentation, release configuration, and CI review. |

Timestamps are preserved from the source exports in UTC (`Z`).

## Sanitization

The repository copies are intentionally **conversation-only sanitized exports**, not complete raw Codex execution traces.

The sanitization process:

- keeps available `user` and `assistant` conversational messages and timestamps;
- removes system/developer instructions, injected environment context, session/model telemetry, token-usage records, hidden reasoning, tool calls, and tool outputs;
- replaces the local developer home path with `$HOME`;
- removes the local username outside normalized paths;
- redacts email addresses, common API-key/token formats, bearer credentials, credential-bearing URLs, private keys, and literal values assigned to common secret variables;
- does not intentionally rewrite the technical content of user or assistant messages beyond those redactions.

A post-sanitization scan found no remaining local developer home paths, email addresses, common Google/OpenAI/GitHub/Slack credential patterns, JWTs, authorization credentials, or private-key markers in these exported conversations.

## Integrity note

These files are sanitized derivatives of real session exports. Missing conversations or messages were not fabricated, backfilled, or assigned invented timestamps.
