# Limitations, Safety and Known-Issues Register (M5 close)

> **Independent educational prototype.** It is not affiliated with, endorsed by, or an official system of HDFC Bank or the RBI. It is **not** production-ready, makes **no** KYC approval or rejection, and gives **no** legal or regulatory compliance determination. It is decision support only, and **every result requires human review**.

## 1. Product limitations (by design or by evidence)
| # | Limitation | Detail |
|---|---|---|
| L1 | **NLI semantic verification is disabled** | The registered S4 selection qualified 0 of 140 configurations. `NLI_MODEL` is empty, and the graph never builds or loads a model. Every claim is `not_verified`, every report with a checklist is DEGRADED, and nothing is shown as MANDATORY after verification. |
| L2 | **The automated conflict scan does not run in production** | It needs a scorer. Reports say "No automated check for conflicting sources was performed", never that no conflict exists. Conflicts between sources may exist undetected. |
| L3 | **Human review is always required** | `human_review_required` is always true (model- and DB-enforced). No outcome or field expresses approval or rejection. |
| L4 | **No automated decision** | The system does not approve or reject applications, verify identities against government databases, validate real PAN/Aadhaar records, or determine legal compliance. |
| L5 | **Review cases are open-only** | M4 records open cases; there is no decision, resolution or dismissal workflow. Reviewer decisions need authentication (future security milestone). |
| L6 | **No authentication** | Possession of a session id is the access model. Not suitable for real users. |
| L7 | **Input screening is a pattern filter** | It blocks ID and financial patterns and a narrow list of override phrases. It is **not** a prompt-injection defence: Prompt Guard and the adversarial suite are not built. Retrieved text is treated as untrusted data and delimited. |
| L8 | **Thin evidence for some entities** | LLP and other non-individual evidence is thin, and NRI evidence is partial. Honest outcomes are often CONDITIONAL or DEGRADED with unresolved items. |
| L9 | **Small, single-annotator evaluation sets** | Retrieval: 20 + 20 queries. Claims: 55 dev cases drafted by the assistant that built the verifier and reviewed by the owner (no inter-annotator agreement). |
| L10 | **Not built** | The 50-case benchmark and observability (M6), the Document Intelligence and Agent Observability screens (moved from M5 to M6), PDF upload and export, deployment (M7), LangSmith tracing, a rules layer, a regeneration loop. `PROMPT_GUARD_MODEL`, `MAX_REGENERATIONS` and `LANGSMITH_*` are reserved settings with no implementation. (The frontend was listed here at M4 close; it was built in M5.) |
| L11 | **Frontend: local only, no authentication** (M5) | A Vite dev server on `127.0.0.1:5173`; no production build is deployed. Anyone holding a session or report id can open it (L6). |
| L12 | **Frontend: browser-local session list** (M5-D4) | Session ids live in the tab's `sessionStorage`: they survive reloads and are gone when the tab closes. The Dashboard lists only this tab's sessions; it is not a history. "Forget sessions" never touches the server. |
| L13 | **Report without source text** (M5) | The API returns citations (document key, pages) but no passage text, so the report cannot preview sources. Page references are not re-checked (K5). |

## 2. Known issues (recorded, not fixed in M4)
| # | Issue | Evidence | Status / owner decision needed |
|---|---|---|---|
| K1 | **The MANDATORY gate does not encode labelling rule 10** (no entity → no obligation) | S4.2 rule trace: m4d-008, m4d-011 | Deferred S2 defect; any fix needs a new, separately approved protocol |
| K2 | **Modality and polarity are analysed per window, not per sentence naming the item** | m4d-052 ("shall" of a neighbouring item outranks "may"), m4d-024 | Deferred S2 defect |
| K3 | **The claim's own polarity is not part of the MANDATORY gate** | m4d-019 ("need not obtain") | Deferred S2 defect |
| K4 | **Labelling rule 1 (abbreviations) is invisible to the gate** | m4d-004 | Deferred S2 defect |
| K5 | **Trust-run citation/page mismatch** (pre-existing M3) | A retrieved child chunk (stored pp. 8–9) is cited with its expanded parent paragraph's range (pp. 4–15), so `check_report` reports `citation_mismatch`. The M3 checklist already holds it before verification. | Found in S8; documented; fix requires an owner decision (citation page source vs acceptance lookup) |
| K6 | **Legacy `fallback_text_missing` wording** | `check_report` expects the "no evidence" fallback on every INSUFFICIENT_EVIDENCE report, but a retrieval-failure report correctly says a technical problem occurred (stored v1 fixture `insufficient_evidence_retrieval_failed`) | Documented separately; not changed |
| K7 | **Environment-dependent unit test** `test_groq_provider.py::test_live_calls_and_cache_are_off_by_default` | The local environment carries the owner's Groq terms-review date (`GROQ_DATA_TERMS_VERIFIED_ON`), which overrides `infra/.env.example` in `Settings`. Live calls stay off. | Baseline; test not modified |
| K8 | **Qdrant tied-score integration failures** `test_dense_golden`, `test_hybrid_dev` | Query m1-14, ranks 2/3: two candidates with tied scores come back in a non-deterministic order. **M5 S1 run (2026-11-13):** the same tied pair (`hdfc_resident_to_nro_conversion` p.3 / `hdfc_nri_account_opening` p.3, identical text, 0.694) flipped at **m1-03 ranks 5/6** instead; `test_hybrid_dev` passed in that run. Which test and query fail varies between runs. | Baseline; tests not modified |
| K9 | **The S4 determinism tolerance (1e-6) is tighter than float32 batch noise** | N1–N3 re-score differences of 3–6e-6 | Recorded; changing it would need a new protocol |
| K10 | **N4 runs in float16 on CPU** | Its pinned config declares `torch_dtype: float16`: p50 1.88 s per pair, 53 min for its part of the S4 run | Recorded cost; relevant only to a future protocol |
| K11 | **Six held-out annotation issues** (m4h-007, -008, -009, -010, -011, -022) | Logged in `data/eval/m4_claims_heldout_review.jsonl` (status text still reads `deferred_to_S8_versioned_correction`; the file was deliberately not edited) | Preserved for a future, separately approved protocol; the held-out set is frozen and unused |
| K12 | **Single-annotator dev set** | 55 cases, owner-reviewed | Selection on it would overfit; the held-out set is the only unbiased check |
| K13 | **A stuck run is not offered a retry in the UI** (M5) | A run left `running` by a server crash can be recovered with `POST /retry` once its lease expires, but the Assistant offers Retry only for `failed` runs; it keeps showing the run as in progress, with Reconnect/Refresh | Owner decision (a future stage) |
| K14 | **Question length limit in the UI** (M5) | The text box allows 2,000 characters, the backend's default `QUERY_MAX_CHARS`; a deployment that configures another value is enforced by the server, not the UI | Confirm against the configured value |
| K15 | **Validator wording on checked v2 items** (M5 finding, backend data) | An item's `uncertainty_label` is set by the M3 validator before verification, so a v2 item whose claim check ran still reads "…proposed, not claim-verified (M4)". The UI shows it verbatim as "Validator note" next to the real claim-check status | Backend wording; owner decision |
| K16 | **Dev-tooling advisory GHSA-g3hv-75wg-m249** (M5) | `minimatch` 10.x through ESLint 10 and typescript-eslint 8 (moderate, lint time only, never shipped). Rechecked at M5 S7: no ESLint or typescript-eslint release depends on the patched minimatch 11.0.1+. Production audit: 0 | Accepted; no forced override; recheck when either package moves to minimatch 11 |

## 3. Privacy and external-LLM restrictions
- **Synthetic data only.** No real customer PII anywhere: not in the repository, logs, workflow state, checkpoints, reports or traces.
- **Screening:** ID and financial identifiers block a request before anything is stored; contact details are redacted.
- **Live LLM calls are off by default:**
  - Groq needs `GROQ_LIVE_ENABLED=true` and a recorded terms-review date. It receives only the screened question, enum context and public corpus text.
  - Gemini needs `GEMINI_LIVE_ENABLED=true` and a terms date, and is limited to approved public blank forms. The workflow never calls Gemini.
  - The LLM cache is off and synthetic-allowlist only.
- **Logging:** payload-logging libraries are held at WARNING, so prompts are not logged.
- **Frontend (M5):**
  - **Browser exposure:** no analytics, no third-party script, font or CDN at runtime, and the referrer policy is `no-referrer`.
  - **Errors:** shown as fixed per-code messages; rejected input is never echoed or logged, and an error's own text is never rendered.
  - **Session ids:** kept only in the tab's `sessionStorage`. They never appear in the URL or the page.
  - **Verified:** S7 browser smoke test step 14 (the console never logged the question, the rejected value or the session id).
- **Secrets** live only in a local, gitignored `.env`. `infra/.env.example` holds no secrets.
- **The NLI scorer is local** (no external call). It is disabled.
- **Provider retention:** Groq retains logs for up to 30 days by default (docs/02).

## 4. Disclaimer
This is an independent educational and portfolio prototype built on public documents and synthetic data. It is not an official HDFC Bank or RBI product, it is not endorsed by either, and it must not be used to make real KYC decisions.
