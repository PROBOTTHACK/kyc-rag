# A3: Understanding and Dialogue Policy

Stage A3 of the HDFC Bank Account Opening Assistant (independent portfolio prototype; not an HDFC Bank or RBI product). Spec: `A1_PRODUCT_SPEC.md` §6 (conversation rules R1–R10) and §8.3 (role of the LLM).

**Scope of this stage:**
- **Built:** deterministic understanding of customer messages, the per-turn dialogue policy, and optional LLM slot extraction, with tests.
- **Not built:** no checklist generation (A4), no API or persistence (A5), no frontend (A6). LangGraph is not used.

## 0. A2 review decisions applied first
- **Accepted corrections:** the owner accepted both A2 corrections. They are now the current specification (`A1_PRODUCT_SPEC.md` §5.4) and machine-readable `item_rulings` in the coverage map (version `a2-2`):
  - organisation signatory photographs: **not established**;
  - entity PAN card copy: **not established**;
  - signatory/partner **PAN No.**: evidenced information (chunks `f51508ae…`, `600f4dd7…`);
  - **Form 60/61**: evidenced, with the form's own condition "for existing customers" (chunk `1c8150b4…`).
- **Validation:** rulings are validated on load. A `not_established` ruling cannot carry evidence. An `evidenced` one must cite opening-form chunks for its categories. A ruling has no classification field.
- **Database check:** the dev-stack test resolves every ruling chunk in Postgres.
- **A0 is unchanged** as the historical audit, with a pointer to §5.4.
- **Standing rules restated in A1 §5.4:**
  - the 26 combinations are corpus evidence coverage, not policy coverage;
  - no policy-supported requirements exist in today's corpus;
  - dense (semantic) retrieval stays the default, and hybrid is experimental;
  - the Recall@5 figures keep their limitations.

## 1. Modules (`backend/app/assistant/`)
| File | Role |
|---|---|
| `profile.py` | Slots (`account_type`, `customer_category`, `nri_scheme`, `holding`, `minor`), slot status (`unknown`, `known`, `not_sure`, `skipped`), origin (`chosen`, `stated`, `corrected`), the pending question, and history. The state is frozen, serialisable data and **never stores message text**. |
| `understand.py` | Deterministic reader. It extracts:<br>- explicit values per slot, plus ambiguities;<br>- hints (residency and the like), residency, organisation-without-kind, unsupported customer types;<br>- conversion intent, item topics, question kind, out-of-scope subjects;<br>- correction cues and control replies.<br>Handles negation ("not a company but an LLP") and short replies. |
| `dialogue.yaml`, `wording.py` | All customer-facing text: questions, A1 chip labels, "What does this mean?" explanations, "not sure" notes, the A1 §7 fallback and notice verbatim. Validated on load; it fails closed if a slot, value or template is missing. |
| `dialogue.py` | The policy. `advance()` is a pure, synchronous transition. `respond_deterministic()` is the no-LLM path. `respond()` adds the optional LLM. |
| `llm_extract.py` | Optional LLM slot extraction through `LLMProvider` (validated output, one re-ask at most, overall timeout). `assistant_llm()` returns a provider only behind the existing Groq gate. |

## 2. Understanding (A3 requirements 1–4, 9)
- **Account type and category** are filled only from explicit wording, e.g. "current account", "my LLP", "NRI", "Pvt Ltd".
  - Bare option words ("savings", "current") count only in short replies, so "my current address" is not a current account.
  - FCNR takes precedence over FD; "limited liability partnership" is never also a partnership firm; "non-resident Indian" is never read as "resident Indian".
- **Ambiguity is asked, never resolved:** two values for one slot ("savings or current account?") leave the slot unknown and trigger its question.
- **Hints become confirmation questions, never values (R7):**
  - "I live in Dubai" leads to "Just to check: is the account for an individual living outside India (NRI / PIO / OCI)?";
  - "I live in India" leads to the resident-individual confirmation;
  - an NRE/NRO mention with no category leads to the NRI confirmation.
  - Unknown places ("Mumbai") are not interpreted.
- **Never guessed:**
  - "My business" is flagged as an organisation of unknown kind, and organisation chips are shown first; all chips stay available.
  - "Foreign national" maps to no category and gets the fallback.
- **Topics for A4:** photograph, PAN, PAN card copy (kept distinct from PAN per A1 §4), no PAN, Form 60, passport, visa/status proof, address proof, identity proof, Aadhaar, signatories, nominee, documents in general.
- **Question kinds:** checklist, item, term.
- **Out of scope:** loans, cards, rates and charges, eligibility or approval, other products.

## 3. Dialogue policy (A3 requirements 5–9)
One step per turn:

| Step | When |
|---|---|
| `ask` | The next missing **material** slot. The account type comes first (R2). Facets come only from the coverage cell's `material_facets`, in the order scheme, holding, minor (R3). |
| `confirm` | A hint, a conflict or an LLM proposal (one per turn). |
| `ready` | The profile is complete for A4. It carries intent, category, account type, coverage state, material facets (`None` = not sure or skipped) and the topics asked. |
| `not_established` | The cell is not covered, with the map's reason, the fallback and the notice. A slot is not asked when no value of it could change the outcome: a salary account needs no category question; a resident individual needs no account question. |
| `cannot_build` | An essential slot was answered "I'm not sure". The fixed explanation of why a reliable checklist can't be built. |
| `fallback` | An out-of-scope question or an unsupported customer type. It gets the fallback and notice, and the next question still rides along. |
| `blocked` | Screening refused the message (identifiers, unusable input, injection). A fixed text; the message is never echoed or stored, and the state is unchanged. |

- **"I'm not sure"** (chip or words) leaves the slot unknown, adds the slot's one-sentence note and moves on. It is never asked again unless the customer chooses to answer.
- **"Skip"** is available only for facets. For an essential slot, it is refused with an explanation, and "I'm not sure" is offered instead.
- **"What does this mean?"** gives the fixed explanation, then the same question again. The explanations state what a choice changes, never banking facts or requirements.
- **Unclear replies are bounded:** one reworded re-ask, then the slot is treated as "not sure". There is no loop and no forced attempt.
- **Corrections (R6):**
  - A profile edit (`CORRECT`), or words with a cue ("Actually it's for my company", "Sorry, I meant…"), replaces the value with origin `corrected`.
  - Without a cue, a contradicting answer becomes a confirmation question instead of a silent overwrite.
  - Facets that stop being material are cleared.
  - History keeps every earlier answer, marked superseded.
- **Item questions** ("Does an LLP need a PAN card?") are kept as topics while the missing slots are asked, then handed over in `ready` (R5).

## 4. Optional LLM (A3 requirements 10–12)
- **When it is consulted:**
  - only through `respond(..., llm=provider)`;
  - only when the rules read nothing at all in a free-text message, with no chip action and no control reply;
  - only for screened, clean text (never blocked or injection-flagged text).
- **What it may return:**
  - The output schema is `{"mentions": [{"slot", "value", "quote"}]}` with `extra="forbid"`. No class, requirement, item, citation or coverage field exists.
  - Values must be enum values of their slot, and only open slots are kept.
  - Each quote must occur verbatim in the message. A slot given two different values is dropped.
- **Every proposal becomes a confirmation question.** Nothing is filled from LLM output directly.
- **Bounded:** at most one re-ask (`generate_validated`) and an overall timeout. Any provider error, timeout or invalid output returns no proposals, and the deterministic path continues.
- **Untrusted input:** the message is wrapped in `<customer_message>` delimiters.
- **No live calls in tests:** the tests use scripted fake providers only. `assistant_llm()` returns `None` unless the existing Groq opt-in gate is open.

## 5. Tests (A3 requirement 13)
| File | Tests | Covers |
|---|---|---|
| `test_assistant_understand.py` | 100 | every account type and category, short replies, negation, ambiguity, no-guess cases, residency hints, facets, topics (PAN number vs PAN card copy), out-of-scope, conversion, controls, correction cues |
| `test_assistant_dialogue.py` | 51 | R1–R10 end to end with no LLM:<br>- ask order and material facets only;<br>- unsupported combinations and the verbatim fallback/notice;<br>- not sure / skip / explain, bounded unclear replies;<br>- corrections (chip, cue, conflict confirm/decline, facet clearing, history);<br>- hints and "my business", unsupported customers, out-of-scope, conversion;<br>- screening, no message text in state, no approval language, input validation, determinism |
| `test_assistant_llm_extract.py` | 18 | LLM not consulted when rules suffice; proposals confirmed before use; quote check; invalid output re-asked once; extra fields refused; provider errors and timeout fall back; blocked text never sent; known slots ignored; delimited request; live provider gated |
| `test_assistant_wording.py` | 9 | complete wording, A1 chip labels, fail-closed loading |
| `test_assistant_coverage.py` | +10 (34 total) | item rulings and their fail-closed validation |

## 6. Checks run for this stage
| Check | Result |
|---|---|
| `make lint` (ruff, black, mypy on 137 files) | clean |
| `mypy --strict app/assistant` | clean (11 files) |
| `make test` | **1752 passed, 1 failed: K7** (`test_groq_provider::test_live_calls_and_cache_are_off_by_default`; the local `.env` sets `GROQ_DATA_TERMS_VERIFIED_ON`). Known baseline; no new failures. |
| `make check-schema` | no new upgrade operations (no schema change) |
| `tests/integration/test_assistant_retrieval_dev.py` | 4 passed (now also resolves the item-ruling chunks in Postgres) |
| `tests/integration/test_hybrid_dev.py` | passed |
| `tests/integration/test_dense_golden.py` | failed at m1-03: **K8**, the known tied-score baseline |
| `make assistant-eval MODE=all` | unchanged from A2: dense 0.733, bm25 0.613, hybrid 0.793 (the 25-case, single-annotator, development-only set) |

## 7. Limits and what A4 receives
- **The vocabulary is a fixed English rule set.** Phrasings outside it are not understood. The customer then gets the question with chips, or the optional LLM proposes a value for confirmation.
- **A residency hint covers only an explicit "abroad" / "outside India" or a short list of countries.** It is only ever a confirmation question.
- **A4 receives:** `ReadyProfile` (intent, category, account type, coverage state, material facets, topics) and the coverage map's cells and `item_rulings`.
- **A4's job:** checklist items, evidence classes, citations and the per-item fallback. Nothing in A3 decides them.

## 8. Demo path
```
cd backend
../.venv/Scripts/python -m pytest tests/unit/test_assistant_understand.py tests/unit/test_assistant_dialogue.py tests/unit/test_assistant_llm_extract.py tests/unit/test_assistant_wording.py
../.venv/Scripts/python -c "from app.assistant.dialogue import respond_deterministic as r, TurnInput as T; from app.assistant.profile import DialogueState as S; s,x=r(S(),T(message='Does an LLP need a PAN card?')); print(x.step, x.question.text)"
```

## Addendum: context-aware follow-ups (owner request, 2026-11-14, before A7)
A browser test showed incompatible contexts being merged: "for a minor" was stored on an LLP current-account profile, and a question about the customer's child was then answered with the LLP checklist. Each message is now read against the conversation so far, inside the existing `_apply_understanding`; no second engine was added. `understand.py` adds three context cues that are never profile values:
- **person:** individual or child;
- **follow-up:** "that", "what about", "does that apply";
- **separate request:** "also", "another" or "separate" with an account.

| Message | Resolution | Profile |
|---|---|---|
| A chip, or words answering the question | as before | changes |
| An answer that doesn't apply to the current account (e.g. "for a minor" on an LLP current account) | never stored. A minor said this way is read as a child (below); anything else gets a "doesn't change anything for …" note | unchanged |
| A person or child mentioned while the holder is an organisation | the `context_conflict` note explains what was understood. The customer category is asked again with individuals listed first and the current holder next (choosing it keeps the checklist). If a correction cue was used ("actually … instead"), the `context_change` note is used instead; a second person-type reply asks whether they live in India or outside India | unchanged until the customer chooses |
| A child mentioned while the holder is an individual | the existing yes/no confirmation of "for a minor" (a hint if unanswered, a conflict if the answer was "no") | unchanged until confirmed |
| A follow-up about the current checklist | `follow_up` note; the reply answers briefly (the documents), not the whole checklist | unchanged |
| A separate request ("I also want an account for my child") | `separate_request` note; this conversation is kept, and the interface offers a separate checklist | unchanged |
| A different account type or category in a follow-up ("what about a fixed deposit?") | the existing conflict confirmation | unchanged until confirmed |
| Not understood at all | out of scope: the prescribed fallback | unchanged |

The **LLM voice** also receives the last three turns, as a delimited `<recent_conversation>` block used only to resolve references. Those turns are never facts: the reply check accepts only the current answer's facts, so an earlier profile's items can't reappear.

When a combination isn't covered, the suggestions offer only the same kind of holder: an individual is never offered an organisation category.

Tests: `tests/unit/test_assistant_context.py` covers the 13 owner scenarios, including reload and PII. `test_assistant_voice.py` checks that recent turns are never treated as facts.

### Superseded in part: greetings and account scenarios (master upgrade, Stages 2–3)
Two rows of the table above changed; the rest stands.

| Message | Resolution now | Profile |
|---|---|---|
| A person or child mentioned while the holder is an organisation (no correction cue) | a **scenario offer**: "a separate account, or change this application?" (`CHOOSE_SCENARIO`; answered by a button or in words). A separate account parks the current one unchanged and starts a fresh one; a correction cue still asks who instead (`context_change`) | unchanged until the customer chooses |
| A separate request ("I also want an account for my child") | a separate account starts at once (`scenario_started`); the current one is parked | the new account starts empty |
| A category of the other kind (a company while discussing a child's account) | the scenario offer, with the named values applied only if chosen | unchanged until the customer chooses |
| A holder or account matching a parked account ("what about the LLP current account?") | an offer to go back (`switch_scenario`, or "no, stay") | unchanged until the customer chooses |
| Hi, hello, yo, thanks, bye, ok | a `social` note answered naturally; a waiting confirmation is asked again; no fallback and no checklist summary | unchanged |

**Who the account is for.** When the customer has said the account is for a person (a child, "for me") and the holder is still open, the category question is asked first with the which-person note, and a residency answer ("we are in India", "we live in Dubai") answers it directly instead of becoming a hint to confirm. For a child, the form's minor question is answered yes. The parked accounts' answers are never used for the new one.

The `context_conflict` and `separate_request` notes, and their wording, were removed. Tests: `test_assistant_context.py` (the daughter flow, going back, greetings, reload), `test_assistant_voice.py` (situations `social`, `new_account`, `choose_account`).
