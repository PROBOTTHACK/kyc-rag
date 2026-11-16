# A7: Final Evaluation and Closure — Plan (for owner review)

> **Planning only.** Nothing in this document has been executed. A7 execution starts only after the owner approves this plan (owner instruction, 2026-11-15). Branch `feat/account-opening-assistant`; no push, merge, rebase, tag or change to `main`.

**Product:** Account Opening Assistant (independent prototype; not affiliated with or endorsed by HDFC Bank or the RBI; synthetic data only). Spec: [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md).
**Baseline:** the branch at `4dc651a`. Last recorded results:
- `make test`: 2,072 passed, 2 known Groq environment failures (K7 and its twin);
- `make test-integration`: 518 passed, 1 failed (K8);
- frontend: 192 tests passed.

**Principles for A7:**
- **The architecture is frozen.** A7 adds evaluation code, datasets, reports and documentation. A product change is made only for a **genuine defect** found by the evaluation. It is then reported, fixed in its own commit with a regression test, and listed in the closure report.
- **No corpus expansion, no new requirements, no coverage change.** Gaps are reported as gaps.
- **Tests are never weakened.** No assertion is loosened to make a run pass, and no failure is hidden or skipped. Known failures are investigated and reported (§9).
- **Synthetic data only.** No live LLM call unless the opt-in procedure (§6) is followed.
- **Reproducible.** Every dataset is a committed file. Every run records its inputs (git commit, corpus manifest hashes, coverage and catalogue versions, model ids for live runs) and writes a dated report.

---

## 1. Evaluation matrix
| # | Area | What is measured | How | Gate |
|---|---|---|---|---|
| E1 | **26 covered combinations** | All 26 cells, expanded to their **55 complete profiles** (NRI savings, current, FD and RD: scheme × holding × minor = 8 each; NRI FCNR/RFC: holding = 2; 21 non-individual cells: 1 each). Each yields a checklist whose structure, classes and citations obey the spec. Plus the **28 not-established cells**, which yield the verbatim fallback. | Scripted API runs (chips) through the real service, Postgres and the stored corpus | Hard |
| E2 | **Resident → NRO conversion** (separate) | Conversion phrasings reach `CONVERSION_ONLY` and cite only `hdfc_resident_to_nro_conversion`; non-conversion requests never use it as a basis | Conversion phrasing set + API runs | Hard |
| E3 | **Conversational intelligence** | Greetings, follow-ups, ambiguous messages, corrections, separate accounts, context retention, off-topic and injection: per-turn expectations | Scripted multi-turn scenarios (§2.3), deterministic; optional live re-run (E11) | Hard (deterministic) |
| E4 | **History and LLM context assembly** | The context the LLM actually receives contains the required history, within budget, in order, and never blocked text | Recording fake provider captures every LLM request | Hard |
| E5 | **Rolling summaries, corrections, reload and restart** | Summaries keep confirmed facts, corrections supersede old values, and reload and restart rebuild identical context | Postgres-backed integration runs | Hard |
| E6 | **Independent account scenarios** | No answer, topic or history leaks between scenarios; switching restores exactly what was parked | Scripted scenarios + seeded random sequences | Hard |
| E7 | **Evidence accuracy and citations** | Every cited chunk re-resolves; source wording is a substring of the chunk text; the page matches; the document is in the cell's source set; OCR text is never quoted; plus a manual check of sampled items against the PDF page | Automated audit + owner-reviewed sample sheet | Hard (automated), owner sign-off (manual) |
| E8 | **Privacy, PII screening, session isolation** | Synthetic identifiers are refused or redacted, and never reach storage, logs, summaries, the LLM or the reply; sessions can't see each other; ids are random | PII message set + log capture + database inspection | Hard |
| E9 | **Idempotency, reliability, failure recovery** | Replays are byte-identical; key mismatch gives 409; concurrent duplicates store once; DB, Qdrant and LLM failures degrade as specified; expiry gives 410 | Fault-injection integration runs | Hard |
| E10 | **Desktop and mobile browser** | Main flows at 1440, 1024, 768 and 390 px: no overflow, no page scroll, no console errors, keyboard-only completion, reduced motion, print | Headless Chrome over CDP (existing scratch tooling, moved into the repo, §8) | Hard |
| E11 | **Live LLM (optional)** | Voice validity rate, fallback rate, factual safety, latency, extraction accuracy | Opt-in procedure (§6) | Soft (reported); safety is hard |
| E12 | **Recorded demo** | One end-to-end video of the scripted demo (§7) | CDP screencast → ffmpeg (already installed locally, **not** a project dependency) | Delivered |
| E13 | **Fresh setup and reproducibility** | A clean clone, with fresh Docker volumes, reaches green tests and the same evaluation results | Isolated compose project (§8, stage A7.7) | Hard |
| E14 | **Documentation and closure** | Closure report, known issues, setup guide, demo guide and an updated spec status | Review checklist (§10) | Owner approval |

---

## 2. Test datasets and scenarios
All datasets are committed, synthetic, versioned (`version:` field) and validated by a unit test (schema, unique ids, no real identifiers).

### 2.1 `data/eval/a7_coverage_profiles.jsonl` (E1, E2)
- **Generated** from `coverage.yaml` by a small script, never hand-written, so it cannot drift from the map:
  - 55 complete profiles for the 26 covered cells;
  - 28 not-established cells;
  - for NRI cells, also "I'm not sure" on each facet, to check the checklist still builds and states what it can't confirm.
- **Conversion cases:** about 12 phrasings (e.g. "convert my resident account to NRO", "I moved to Dubai, change my savings account to NRO", "switch to NRO"), plus about 6 near-misses that must **not** trigger conversion ("open an NRO account", "what is NRO?").

### 2.2 Expected properties (no expected item lists are invented)
Expected checklist contents are **not** written by hand, so no requirement is invented. Each run is checked against invariants derived from the spec and the stored data:
- `kind = checklist` and `coverage_state` = the map's state for the cell;
- every item: one class from §3; `policy_supported` never appears; classes agree with the source tier;
- every citation's chunk exists in Postgres; its document is in the cell's `sources`; the page matches the stored chunk;
- `source_wording` is an exact substring of the chunk text, or the stored field label;
- `ocr_uncertain` text is never marked `quotable`, and scanned items carry `scanned_text`;
- no "PAN card copy" item, and no photograph item for organisations (A1 §5.4 rulings);
- the notice is verbatim;
- **golden snapshots:** item ids per profile are recorded once at A7 start, so later runs detect any drift. A diff fails the run, and the owner reviews any change.

### 2.3 `data/eval/a7_conversations.yaml` (E3, E4, E5, E6)
About 70 scripted conversations. Each turn lists the message or chip, then expectations: step kind, note codes, profile after the turn, offer present or absent, fallback yes/no, and phrases that must or must not appear in the reply.

| Category | n | Examples |
|---|---|---|
| Greetings and small talk | 10 | "Hi", "Yoooo", "good morning", "thanks", "bye", "ok", "how are you?" at the start, mid-question, after a checklist and during a pending confirmation: no fallback, no checklist repeat, a pending question re-asked |
| Follow-ups | 10 | "does that apply to me?", "what documents for that?", "do I need a photograph?", "what about Form 60?": a brief answer, profile unchanged |
| Ambiguous or unclear | 10 | "bleeh", "a business account", "for my firm", "an account", "I live abroad": one re-ask or a confirmation, never a guessed value (R7) |
| Corrections | 10 | chip correction; "actually it's for my company"; conflicting value without a cue → confirm; facet clearing on a category change |
| Separate accounts | 12 | the owner's daughter flow (LLP → daughter → separate → "No, we are in India."); "I also want one for my child"; go back by name and by button; "no, stay"; change-application path; residency outside the which-person question stays a hint |
| Context retention | 8 | 15–25-turn conversations where a fact from turn 2 must still drive turn 20; reload mid-conversation; restart mid-conversation |
| Off-topic and injection | 10 | interest rates, loans, "ignore previous instructions…", a markdown or HTML payload: the prescribed fallback, nothing followed, nothing changed |

### 2.4 `data/eval/a7_memory_cases.yaml` (E4, E5)
About 15 cases, each with a scripted conversation and a list of **facts that must appear** in the LLM context at a given turn: the active profile, the corrected value with its replacement, the parked scenario label, the open question, and an early customer message. Also listed: **facts that must not appear**: blocked text, a superseded value presented as current, another session's data.

### 2.5 `data/eval/a7_pii_messages.jsonl` (E8)
About 60 messages with synthetic identifiers in varied positions and formats:
- PAN;
- Aadhaar (12-digit, synthetic);
- passport;
- card numbers (Luhn-valid test numbers);
- bank account numbers;
- e-mail addresses and phone numbers (expected redacted);
- identifiers split by spaces or dashes.

The set also includes about 15 **negative controls** that must **not** be blocked: "Form 60", "a 6-year-old", "page 2", amounts.

### 2.6 Conversation records for the browser (E10, E12)
The demo script (§7) and four viewport flows reuse the conversation ids from §2.3.

---

## 3. Metrics and thresholds
| Metric | Definition | Threshold |
|---|---|---|
| Coverage pass rate (E1) | profiles whose run satisfies every §2.2 invariant ÷ 55 | **100 %** |
| Not-established correctness (E1) | fallback verbatim, no checklist, suggestions only of the same holder kind ÷ 28 | **100 %** |
| Conversion routing (E2) | correct `CONVERSION_ONLY` on positives; no conversion basis on near-misses | **100 %** |
| Citation validity (E7) | cited chunks that re-resolve, have a matching page, belong to the cell's sources and contain the wording ÷ all citations | **100 %** |
| Manual citation check (E7) | owner-reviewed sample of 30 items (15 from NRI checklists, 15 from organisation checklists, seeded random): page and wording match the PDF | **30/30**; any miss is a defect |
| Classification invariants (E1, E7) | `policy_supported` count; inferred document items; organisation photo items | **0** each |
| Conversation turn accuracy (E3) | turns meeting all expectations ÷ all turns (deterministic) | **100 %** |
| Greeting safety (E3) | greeting or small-talk turns that trigger a fallback or a checklist repeat | **0** |
| Context recall (E4) | required facts present in the captured LLM context ÷ required facts | **100 %** |
| Context exclusions (E4) | forbidden facts present (blocked text, other session, superseded-as-current) | **0** |
| Context budget (E4) | captured contexts within `ASSISTANT_CONTEXT_TOKENS` (+ fixed profile part) | **100 %**; no message cut |
| Summary fidelity (E5) | confirmed values in the summary = state's known values; every correction recorded with its replacement | **100 %** |
| Reload and restart determinism (E5) | context rendered after reload or restart = before | **100 %** identical |
| Scenario isolation (E6) | leaked slot values or topics across scenarios over 200 seeded random sequences | **0** |
| PII refusal (E8) | identifier messages blocked (or e-mail/phone redacted) ÷ set | **100 %** |
| PII leakage (E8) | identifier strings found in DB rows, summaries, log capture, LLM requests or responses | **0** |
| PII false positives (E8) | negative controls blocked | **0** (any found is reported, not hidden) |
| Session isolation (E8) | cross-session reads that succeed; id collisions in 10,000 ids | **0** |
| Idempotency (E9) | replay byte-identical; mismatch 409; 10 concurrent duplicates → 1 stored turn | **100 %** |
| Failure handling (E9) | DB down → 503; checklist store failure → `technical_error`, nothing stored, retry succeeds; LLM error, 429 or timeout → deterministic reply | **100 %** |
| Deterministic latency (E9) | server time per turn, no LLM, local stack, 500 turns | p95 **≤ 750 ms**; p99 reported |
| Browser checks (E10) | overflow, page-fit, console errors, keyboard completion, print, at 4 widths | **0** failures |
| Live voice validity (E11) | live replies accepted by `check_reply` ÷ live replies | **≥ 80 %** (soft; the rest fall back deterministically) |
| Live factual safety (E11) | accepted live replies that contain a document or number not in the facts (re-audited offline), or approval or obligation wording | **0** (hard) |
| Live latency (E11) | turn time with the live voice | p95 **≤ 12 s** (bounded by the 10 s timeout) |
| Live extraction (E11) | LLM-proposed slot values that match the scenario's intended value, always confirmed first | reported; wrong proposals must never be committed without a "yes" |
| Reproducibility (E13) | fresh-clone evaluation results = branch results (same item ids, pass rates) | **identical** |

Every report prints these numbers, together with the run's commit, manifest hashes, coverage and catalogue versions, and (live only) model ids.

---

## 4. Memory evaluation details (owner's five checks)
1. **Relevant history is supplied to the LLM.** A recording `LLMProvider` (test-only, implementing the existing interface) captures every request during the §2.4 cases. The check parses the delimited `<recent_conversation>` block and asserts the required facts are present.
2. **Summaries preserve important information.** After forcing summarisation (small budget, few recent turns), `assistant_sessions.summary` is read from **Postgres** and compared with the state: confirmed values, corrected values with their replacements, open question, other scenarios, earliest messages.
3. **Corrections supersede outdated information.** After a correction, the old value appears only in `corrected` and never in `confirmed` or in the profile of the context.
4. **Separate scenarios remain isolated.** The parked scenario appears only as a label under "other accounts", never as the active profile; its values never appear in the active scenario's answers (E6 random sequences).
5. **Deleted conversations cannot be recovered.** Deleting a session row (the documented manual delete) removes its turns, keys and summary by cascade. The check then:
   - reads every table for the session id and finds no row (including `summary`);
   - finds no cache entries in Redis or the LLM cache keyed to it;
   - gets 404 from the API for the old id;
   - finds that a new session never sees the old content.

   A gap, if found, is a defect.

---

## 5. Evidence accuracy procedure (E7)
1. **Automated audit:** for every checklist produced in E1:
   - re-resolve each citation's chunk in Postgres;
   - compare document key, page and section;
   - check that the source wording is a substring of the chunk text;
   - check the document is in the cell's `sources`;
   - check the class agrees with the source tier;
   - check that `quotable` = `text_reliability ∈ {native, ocr_reliable}`.
2. **Manual check:**
   - a seeded sample of 30 items goes into `docs/assistant/a7/citation_review.md`, with each item's title, document, page and quoted or approximate text;
   - the owner (or I, with the owner confirming) opens the local PDF page and marks each row match or mismatch;
   - results go into the closure report.
3. **Retrieval re-run:** `make assistant-eval MODE=all` is re-run for comparison with A2. It is reported with A2's limitations (25 cases, one annotator, development set only). It is **not** a gate.

---

## 6. Live LLM test procedure (E11; opt-in, synthetic only)
1. **Preconditions:**
   - the owner confirms the run;
   - `.env` has `GROQ_API_KEY`, `GROQ_DATA_TERMS_VERIFIED_ON` and the model ids;
   - the run is started with `GROQ_LIVE_ENABLED=true` **on the command line**. The session environment currently forces it off, which is the safe default.
   - Unit tests never call Groq; this is a separate command (`make a7-live`).
2. **Inputs:** only the §2.3 conversation set and the §2.5 PII set (to prove blocked text never reaches the provider). No other text.
3. **Budget and safety:**
   - the existing rate limiter (6,000 TPM free tier) and 10 s timeout;
   - at most 2 retries (CLAUDE.md);
   - the run aborts on 3 consecutive provider errors;
   - an estimated token budget is printed before starting;
   - no LLM cache unless `LLM_CACHE_ENABLED` is set, which it isn't by default.
4. **Captured:**
   - per turn: the request size, accept or fallback with its reason code, latency, and the final reply;
   - never the API key; the raw prompt is stored only as a hash plus its delimited fact block, which contains no customer PII.
5. **Offline re-audit:** every accepted live reply is re-checked by a stricter offline audit for documents and numbers not in the facts, and for wording outside the voice rules. Any violation that `check_reply` let through is a **defect**: it is reported, the validator is fixed, and a regression test is added.
6. **Report:** `docs/assistant/a7/live_llm_report.md`, with the model ids, the date and the §3 live metrics.

---

## 7. Browser demo script (E10, E12)
A ~4-minute recording at 1440×900. Steps 7 and 9 are re-shot at 390×844.
1. **Welcome:** the disclaimer and the real-data warning → "I understand — start".
2. "Hi" → a natural greeting, then the account question.
3. Choose **Current account**, then **A limited liability partnership (LLP)** → the checklist summary in the chat → **Checklist (n)** → expand Form 60/61: the approximate reading, the scanned-text warning, and the source with page and passage.
4. "Do I need a copy of the PAN card?" → it can't be confirmed (Group C); there is no PAN-card requirement.
5. "How can I open an account for my daughter who is six?" → offer → "Ask about a separate account" → "No, we are in India." → an honest fallback: no resident opening form in the corpus.
6. "what about the LLP current account?" → "Yes, go back to it" → the LLP checklist is back, unchanged.
7. **Your answers:** progress, Change, the other accounts.
8. "my PAN is ABCPE1234F" (synthetic) → refused, never echoed.
9. Reload → the conversation is restored.
10. Print preview of the checklist.
11. Start again → a new NRI flow: "I live in Singapore and want a savings account" → confirm → NRE → joint → minor no → NRI checklist.

The recording uses the deterministic voice by default. A second take with the live voice is optional and needs the §6 opt-in. Output: `docs/assistant/a7/demo.mp4` (if size allows, under ~20 MB, otherwise kept outside git with a checksum), plus key-frame PNGs and `A7_DEMO.md` (how to reproduce).

---

## 8. Proposed files and execution stages
**New files:**
- **Evaluation code** (`backend/app/assistant/a7/`), outside the product request path:
  - `runner.py`: drives the service and API;
  - `invariants.py`: §2.2 checks;
  - `memory_probe.py`: the recording provider;
  - `pii_probe.py`;
  - `faults.py`;
  - `report.py`;
  - `generate_profiles.py`.
- **Datasets:** `data/eval/a7_coverage_profiles.jsonl`, `a7_conversations.yaml`, `a7_memory_cases.yaml`, `a7_pii_messages.jsonl`.
- **Tests:**
  - `backend/tests/unit/test_a7_datasets.py` (schema and synthetic-only checks);
  - `backend/tests/integration/test_a7_*.py` (Postgres-backed memory, deletion, idempotency and faults).
- **Browser tooling:** `frontend/e2e/` (or `scripts/browser/`), the CDP driver moved from the scratchpad into the repo. It uses Python's `websocket-client`, which is **not** in the project. Options:
  - (a) add it as a dev dependency;
  - (b) a small Node CDP client using Node's built-in `WebSocket` (Node 26). **Recommended:** no new dependency.

  **Needs your choice.**
- **Makefile:** `a7-eval`, `a7-live` (opt-in), `a7-browser`, `a7-demo`.
- **Reports:**
  - `docs/assistant/a7/`: coverage, conversations, memory, privacy, reliability, browser, live and citation review;
  - `docs/assistant/A7_CLOSURE_REPORT.md`;
  - `docs/assistant/A7_DEMO.md`;
  - an updated `docs/handoff/04-limitations-and-known-issues.md` (new rows only; existing rows unchanged).

**Stages** (each: run, report, one commit, summary; continue to the next only with approval):

| Stage | Scope |
|---|---|
| A7.1 | Harness and datasets (generated profiles, conversations, memory cases, PII set) and dataset tests. No product change. |
| A7.2 | E1, E2 and E7: coverage, conversion and evidence audit; citation review sheet prepared for the owner. |
| A7.3 | E3–E6: conversation, memory, summaries, reload/restart and scenario isolation (Postgres-backed). |
| A7.4 | E8 and E9: privacy, isolation, idempotency, faults and latency. |
| A7.5 | K7 and K8 investigations (§9): findings and proposed fixes, for owner decision. |
| A7.6 | E10 and E12: browser matrix and the recorded demo. E11 (live) only if the owner opts in. |
| A7.7 | E13: fresh clone in a temporary directory, a **separate compose project name and volumes** (the existing databases are never touched), venv, `npm ci`, migrate, place the PDFs, ingest, run everything, compare results. |
| A7.8 | E14: closure report, docs, known issues; final full validation run. |

Any defect found becomes its own commit: the fix and a regression test, referenced in the stage report.

---

## 9. Known failures: separate investigations (A7.5)
- **K7** (`test_groq_provider.py::test_live_calls_and_cache_are_off_by_default`):
  - **Hypothesis:** the test passes `_env_file=infra/.env.example`, but `GROQ_DATA_TERMS_VERIFIED_ON` is also set in the **process environment**, which outranks env files in pydantic-settings.
  - **Plan:** confirm by running the test with that variable removed from the environment and with it present; record both.
  - **Proposed fix (owner decision):** the test isolates the environment (`monkeypatch.delenv` for the GROQ and LLM variables), so it checks the **code defaults** it claims to check. Its assertions are unchanged. This makes the test stricter about its subject.
- **K7b** (`test_app_imports_without_the_groq_sdk_or_a_key`, not in the K-list yet):
  - **Cause:** it strips GROQ variables from the subprocess environment, but `get_settings()` still reads the repo-root `.env`, where `GROQ_LIVE_ENABLED=true` was set for the local demo at the owner's request. The test then sees `True`.
  - **Plan:** confirm, add it to the known issues, and propose the same isolation (point the subprocess at a settings source without the local `.env`, or set `GROQ_LIVE_ENABLED=false` explicitly in its environment). Its assertions are unchanged.
- **K8** (`test_dense_golden`, sometimes `test_hybrid_dev`):
  - **Hypothesis:** two chunks with identical text and score (NRO conversion p3 / NRI form p3, 0.694) are returned in an unstable order because Qdrant has no secondary sort key.
  - **Plan:**
    - run both tests 30× and record the flip frequency and which queries flip;
    - inspect the retrieval code for a tie-break;
    - check whether the **assistant's** retrieval (`app/assistant/retrieval.py`) is affected or only the legacy M1 path.
  - **Possible outcomes (owner decision):**
    - (a) add a deterministic tie-break (score, then document key, page and chunk id). This is a genuine determinism defect fix, with a regression test.
    - (b) if only legacy code is affected and the owner prefers to leave legacy unchanged, keep K8 documented with the measured rate.

    In neither case is the golden file or its assertion loosened.

---

## 10. Known limitations (documented honestly in the closure report)
1. **Resident individuals:** the corpus has **no HDFC opening form for individuals living in India**. Every resident combination (savings, current, salary, deposits), including the daughter-in-India example, gets the prescribed fallback with an explanation. The corpus is not expanded and nothing is inferred. Adding a form would need a new ingested PDF, an audit update and owner approval.
2. **Not-established cells:** salary accounts (all categories) and non-individual savings.
3. **Dated forms:** the non-individual form is a 2015 print and scanned (OCR-uncertain), so all 21 organisation cells carry scanned-text caveats. No current HDFC policy is in the corpus, so `policy_supported` is never produced.
4. **Corpus provenance and reproducibility:** the source PDFs are **gitignored** and the manifest records **SHA-256 hashes but no source URLs**. A fresh clone cannot rebuild the corpus without the owner's local PDFs. A7.7 verifies the hashes; the closure report states this. Adding public source URLs to the manifest is an owner decision and is not part of this plan.
5. **Retrieval evaluation:** a 25-case, single-annotator development set with no held-out split (A2).
6. **Live LLM:** free-tier rate limits (6,000 TPM) cause fallbacks to the deterministic reply under load. The voice is optional and never decides content.
7. **No authentication:** a session id is the only handle, acceptable only because the data is synthetic. There is no automatic retention job: rows are kept until deleted by hand.
8. **Fonts** load from Google Fonts at runtime.
9. **Legacy code** (M0–M5) is preserved and unused by the assistant; its K7/K8 failures are reported as above.
10. **Not production-ready, not endorsed, not a KYC or eligibility decision.**

---

## 11. Final acceptance criteria (A7 closes only when all hold)
1. Every **hard** threshold in §3 is met, or each miss has a documented defect fix, re-run and regression test.
2. The owner has signed off the 30-item citation review.
3. `make lint`, `make check-schema`, `make test`, `make test-integration`, `make web-test`, `make web-lint`, `make web-build` and `check:api` all pass. The **only** permitted failures are K7, K7b and K8, each with its investigation report and the owner's decision recorded. Nothing is skipped, deleted or loosened.
4. The browser matrix passes at 4 widths with zero console errors, and the demo video and `A7_DEMO.md` exist.
5. The fresh-clone reproduction (A7.7) gives identical evaluation results.
6. If opted in, the live run shows zero factual-safety violations. If not, the closure report says so.
7. `A7_CLOSURE_REPORT.md`, the known issues, the README demo section and the PRODUCT_SPEC §11 status are updated. The "Account Opening Assistant" name is used consistently, with no endorsement wording.
8. Every commit is on `feat/account-opening-assistant`. Nothing is pushed, merged, rebased or tagged, and `main` is untouched.

---

## 12. Decisions needed before execution
1. **Browser tooling:** a Node CDP client with no new dependency (recommended), or add `websocket-client` as a Python dev dependency.
2. **Live LLM run (E11):** include it, or skip it and state so.
3. **K7/K7b:** approve the environment-isolation fix (assertions unchanged), or keep them as documented failures.
4. **K8:** approve a deterministic tie-break if the investigation confirms it, or keep it documented.
5. **Demo video:** commit it (if ≤ ~20 MB) or keep it outside git with a checksum.
6. **Citation review:** whether you will mark the 30-item sheet yourself, or I prepare it and you confirm.
