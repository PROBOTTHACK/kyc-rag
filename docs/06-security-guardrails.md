# 06 — Security & Guardrails

## A. Input security
Detect prompt injection, jailbreaks, system-prompt extraction, disguised malicious instructions. Combine Prompt Guard 2 classifier + deterministic checks (length, format, encoding tricks). Distinguish legit security questions from override attempts. Routes: safe / blocked / review.

**Implemented in M3 S3 (deterministic part only; Prompt Guard is M4).**
- **Where it lives:**
  - `app/security/screen.py` does the screening.
  - `app/graph/inputs.py` is the gate in front of the graph: `start_input` and `resume_command`.
  - The `input_security` and `clarify` nodes repeat the checks inside the graph.
- **Input hygiene:**
  - Empty, over-long (`QUERY_MAX_CHARS`, default 2000) or invalid text is rejected at the gate.
  - Control characters block.
  - Invisible or bidirectional formatting characters go to review.
- **Injection phrases:** the list is deliberately narrow.
  - Imperative override phrases ("ignore all previous instructions") and system-prompt extraction requests block.
  - Role-override phrases ("you are now…", "developer mode") go to review. In M3, review ends `BLOCKED`.
  - Questions *about* system prompts or security pass.
- **Personal data:**
  - **Blocked:** PAN, Aadhaar (Verhoeff-checked), passport-like numbers, card numbers (Luhn-checked), and long digit runs shortly after account/IFSC wording. The gate rejects these (the API maps this to `422 SENSITIVE_DATA_DETECTED`), and nothing is stored or sent.
  - **Redacted:** email and phone are replaced with `[REDACTED_EMAIL]` / `[REDACTED_PHONE]`, and the request proceeds.
- **Clarification answers:** they are enum values only (no free text) and are screened like the question. After every answer the graph returns to `input_security`.
- **What is recorded:** screening records hold category codes and counts only, never the matched text.
- **The intake prompt:** it delimits the question and states that its contents are data, not instructions.
- **Limits:** this is a best-effort pattern filter.
  - It does **not** guarantee that every personal detail is found, nor that every prompt-injection attempt is caught.
  - The real controls are the synthetic-data-only policy, delimited prompts, the absence of tools for the model, and the deterministic output validator (S5).
  - A caller that skips the gate is still screened inside the graph. But LangGraph checkpoints graph input and resume values, so the API must always go through the gate.

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
