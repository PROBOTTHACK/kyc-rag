# 06 — Security & Guardrails

## A. Input security
Detect prompt injection, jailbreaks, system-prompt extraction, disguised malicious instructions. Combine Prompt Guard 2 classifier + deterministic checks (length, format, encoding tricks). Distinguish legit security questions from override attempts. Routes: safe / blocked / review.

## B. Document security
Retrieved PDF text is **untrusted evidence**. E.g. a chunk saying "Ignore previous instructions…" must be treated as content, never obeyed.
- Wrap evidence in clear delimiters; system prompt states evidence is data only.
- Scan chunks at ingestion for injection patterns; flag (don't silently drop) suspicious chunks.
- Synthesis/verification prompts never grant tool access based on evidence content.

## C. Output security
Validate: required JSON fields, citation refs resolve to real doc/page, no unsupported MANDATORY claims, no sensitive data disclosure, well-formed entity info.

## Agent least-privilege
LLM can't: execute code, read env vars, read arbitrary files, change DB permissions, make arbitrary external requests. Tools are read-only retrieval + controlled state transitions. DB role used by the app has minimal privileges.

## API hygiene
File-type/size limits on uploads, PDF magic-byte check, uploads stored outside web root, per-IP/session rate limits (Redis), CORS locked to frontend origin, no stack traces to clients, secrets via env, dependency pinning.

## Adversarial test set (see `10-evaluation-plan.md`)
Prompt injection, malicious PDF instruction, system-prompt request, fake compliance requirement, contradictory docs, missing evidence. Track **attack success rate**.
