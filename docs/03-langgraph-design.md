# 03 — LangGraph Design

Single state machine, specialized nodes. A node may use an LLM, deterministic Python, or an external service. Checkpointer: Postgres. Status (M3 complete, 2026-11-12): the workflow runs end to end through the API; claim verification (NLI) and review cases are M4 (`docs/milestones/M4_PLAN.md`); Prompt Guard moves to the Future security milestone — number TBD (M4 decision D1). Progress events (M3 S7): the durable `graph_runs.node_events` in Postgres, streamed over SSE, with Redis pub/sub as a best-effort wake-up only. Tracing: LangSmith.

## State
> **As implemented (M3 S2): `app/graph/state.py`.** It follows `docs/milestones/M3_PLAN.md` §4–§5, which supersedes the sketch below:
> - **JSON-safe values:** every value is JSON, and nested values are Pydantic model dumps, so checkpoints never deserialise application classes.
> - **Outcome vocabulary:** `final_status` is replaced by the single report `outcome` (`BLOCKED`, `INSUFFICIENT_INFORMATION`, `INSUFFICIENT_EVIDENCE`, `DEGRADED`, `CHECKLIST_REVIEW_REQUIRED`, in precedence order), plus the separate run lifecycle `RunStatus`.
> - **Claim fields (M4 S1, models only; the node arrives in S5):** `claim_results` (one `RequirementVerification` per validated requirement), `verification_meta` and `unverified_items`. They are not input keys, so a caller can never pre-fill them. The models enforce the never-upgrade invariant and the M4 MANDATORY rule themselves (docs/milestones/M4_PLAN.md §E, §I).
> - **Routing:** `app/graph/routing.py`. A blocked or review security status goes to `finalize`, not to a separate refusal node (M3 plan revision 2).
> - **Security, intake and clarification (M3 S3):** these are real nodes.
>   - **`input_security`:** deterministic and monotonic. An input cannot declare itself safe, and a block is never relaxed.
>   - **`entity_intake`:** async. It makes one LLM call on the first pass only.
>   - **What intake accepts:** values in the closed vocabularies, backed by words in the question; nothing is defaulted.
>   - **`clarify`:** uses `interrupt()` and `Command(resume=...)`, with enum-only answers. Every round counts, up to `MAX_CLARIFICATION_ROUNDS`.
>   - **After an answer:** the graph returns to `input_security`.
>   - **Running it:** the graph runs with `ainvoke`/`astream`.
> - **Retrieval, forms and association (M3 S4):** these are real nodes.
>   - **Reads:** sync nodes reading through `GraphDeps.retriever` / `GraphDeps.corpus` (`app/graph/corpus.py`).
>   - **Retrieval:** an entity pass, plus an entity-group pass when no primary-capable chunk is found.
>   - **Evidence status:** one of four, in `retrieval_meta.evidence_status` (`found` / `partial` / `none` / `retrieval_failed`).
>   - **Forms:** stored schemas only, never Gemini.
>   - **Links:** relationships only.
>   - **No new edges.**
> - **Synthesis, validation and finalize (M3 S5):** these are real nodes.
>   - **`compliance_synthesis`:** async, one call plus one re-ask, under the node time limit. Its prompt holds profile values, delimited evidence, bounded form labels and gap codes, and fits the Groq token budget.
>   - **`output_validation`:** deterministic and authoritative.
>   - **`finalize`:** writes one insert-only report per run (id = uuid5 of session and run) through `GraphDeps.reports`, with a deterministic user message: the fallback text when nothing is supported, failure reported apart from "no information", and the disclaimer.
>   - **No new edges.**
> - **Execution (M3 S6):**
>   - **Checkpointer:** the graph runs in the API process with `AsyncPostgresSaver` (the `langgraph` schema), one thread per question.
>   - **Single submission point:** `WorkflowService._execute` is the only place that submits input. It submits only a `start_input` state, a `resume_command` result or `None` (retry from the last checkpoint).
>   - **Recovery:** explicit, through `/retry` after the lease expires.

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
6. **claim_verification** (M4, `docs/milestones/M4_PLAN.md`; supersedes the earlier sketch here) — runs **after** `output_validation` (decision D8), only on items that survived citation validation: typed atomic claims (`policy_obligation`, `form_request`, `supporting_statement`, `explanatory_claim`) -> premise windows from the stored chunk text -> deterministic statement analysis (modality, polarity/negation, subject, item match, entity scope) -> local NLI (premise = evidence, hypothesis = claim) -> a categorical decision table (no combined confidence; scores are uncalibrated) -> downgrade-only effects. No regeneration loop (D3) and no separate `human_review` node: unsupported items move to `unverified_items`, contradictions and source conflicts become open review cases, and every report already requires human review. Verification failure is `not_verified` (DEGRADED), never "supported". **Status (M4 S5):** wired as `output_validation -> claim_verification | finalize` (only a non-empty checklist is verified). It runs with NLI disabled, because S4 selected no configuration, so every report with a checklist is currently DEGRADED with all claims `not_verified`.
7. **output_validation** — valid JSON, valid citation refs (doc/page exist), no unsupported MANDATORY, entity classification sane, uncertainty labels present, no sensitive disclosure. Persist report.
8. **human_review / refusal** — create review case (see `05`), or return safe refusal.

## Loop & failure control
- `regeneration_count <= 2`; global graph step limit; per-node timeout.
- On LLM invalid JSON: retry once with repair prompt, then degrade (mark claim unverified).
- On provider 429/5xx: backoff -> fallback model -> degrade gracefully with explicit status.
- Checkpoint after every node so any crash is resumable.
