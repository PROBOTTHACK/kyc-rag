# 03 — LangGraph Design

Single state machine, specialized nodes. A node may use an LLM, deterministic Python, or an external service. Checkpointer: Postgres. Progress events: Redis pub/sub. Tracing: LangSmith.

## State
```python
class KYCState(TypedDict, total=False):
    # request
    session_id: str; query: str; conversation_history: list
    # security
    security_status: str  # safe | blocked | review
    security_reason: str | None
    # intake
    entity_type: str | None; account_type: str | None
    entity_profile: dict; missing_fields: list; intake_complete: bool
    # retrieval
    metadata_filter: dict; retrieved_chunks: list; retrieval_metadata: dict
    # form analysis
    form_analysis_required: bool; form_analysis: dict
    # synthesis
    claims: list; draft_response: dict; regeneration_count: int
    # evaluation
    verified_claims: list; unsupported_claims: list; contradicted_claims: list
    unresolved_claims: list; evaluation_results: list
    # outcome
    final_status: str  # completed | blocked | review_required
    report_id: str | None
```
State rules: no secrets; store file references not binaries; isolate by session; persist only what's needed to resume; log state transitions; apply retention limits.

## Nodes
1. **input_security** — length/format validation, Prompt Guard classifier, deterministic checks, sensitive-info screening. Output `{security_status, security_reason}`. Route: safe->intake, blocked->refusal, review->human_review. Don't flag legit security questions just for containing "system prompt".
2. **entity_intake** — LLM structured output (Pydantic) -> `entity_profile`, `missing_fields`, ambiguity flags, `metadata_filter`. **Never guess missing values.** If incomplete: LangGraph `interrupt()` with targeted questions; resume with user's answer via `/sessions/{id}/resume`.
3. **policy_retrieval** — embed query, Qdrant filtered dense search, BM25 keyword search (`rank_bm25`, in-process index built from Postgres chunks), RRF fusion, optional cross-encoder rerank, dedupe, package with source metadata. Output chunks from real retrieval only.
4. **form_analysis** (conditional) — skip for policy-only questions. Prefer cached validated form schema (Postgres + Redis); call multimodal model only if absent/new hash. Output fields, checkboxes, signatures, declarations, annexures, `unresolved_items`.
5. **compliance_synthesis** — inputs: entity profile, evidence, form schema, rules. Produces Pydantic-validated checklist. Each requirement: name, applicability (MANDATORY/CONDITIONAL/OPTIONAL/UNCLEAR; enum `Applicability` in app/schemas/taxonomy.py), explanation, evidence refs, source doc+page, confidence (evaluation confidence, not legal certainty), review status.
6. **claim_verification** — extract atomic claims -> match evidence -> NLI (entail/contradict/neutral) -> check source applicability & rules -> combined confidence (never NLI alone). Routing:
   - SUPPORTED -> output_validation
   - UNSUPPORTED -> regenerate (max 2) then mark unverified
   - CONTRADICTED -> human_review
   - UNRESOLVED -> mark Unverified
   Also detects **missing** evidence, not just false statements.
7. **output_validation** — valid JSON, valid citation refs (doc/page exist), no unsupported MANDATORY, entity classification sane, uncertainty labels present, no sensitive disclosure. Persist report.
8. **human_review / refusal** — create review case (see `05`), or return safe refusal.

## Loop & failure control
- `regeneration_count <= 2`; global graph step limit; per-node timeout.
- On LLM invalid JSON: retry once with repair prompt, then degrade (mark claim unverified).
- On provider 429/5xx: backoff -> fallback model -> degrade gracefully with explicit status.
- Checkpoint after every node so any crash is resumable.
