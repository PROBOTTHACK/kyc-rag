# 11 — Observability, Reliability, Privacy, Limitations

## Observability (LangSmith + own tables)
Per request capture: session ID, graph node, LLM model, latency, retrieval count, token usage, retry count, evaluation result, final status. Tag runs with session/config name. **Never log PII or secrets.** Sample traces to respect free quota. Mirror key fields into `graph_runs` so the in-app Observability screen works without LangSmith.
Demo moment: show a claim flagged by the evaluator -> graph routes back -> trace shows what changed.

## Reliability
Timeouts, rate-limit handling (Redis buckets), exponential backoff with jitter, model fallback, invalid-JSON recovery, retrieval-failure handling, checkpointing, max retry limits, graceful degradation with explicit status, health checks for Postgres/Redis/Qdrant.

## Privacy
Synthetic customer data only; no real KYC records; check each external API's data-use terms before sending any document (free tier ≠ safe for confidential data); env vars for credentials; restricted uploads; retention + deletion jobs; don't log sensitive info.

## Limitations (put in README + UI)
Does not: approve/reject real applications; verify identity against government databases; validate real PAN/Aadhaar; make legal/regulatory decisions; guarantee compliance; replace compliance officers; access bank internal systems.
