# A5: Assistant API v2 and persistence

Stage A5 of the HDFC Bank Account Opening Assistant (independent portfolio prototype; synthetic data only). It puts the A3 dialogue and the A4 checklist behind a small HTTP API and stores each conversation in Postgres. It adds no new requirements, sources, coverage or profile questions.

## 1. Endpoints (`/api/v2/assistant`)
| Method and path | Body | Success | Errors |
|---|---|---|---|
| `POST /sessions` | none | **201** `{session_id, turn: 0, answer}`: the opening question | 503 |
| `POST /sessions/{id}/turns` | `{message?, action?, slot?, value?}` and the header **`Idempotency-Key`** (required, 1–200 characters) | **200** `{session_id, turn, answer}` | 400, 404, 409, 410, 422, 503 |
| `GET /sessions/{id}` | none | **200** `{session_id, turn, profile, turns[], answer}`, to reload a conversation | 404, 410, 503 |

- **A turn body** matches the A3 `TurnInput`: a free-text `message`, a choice (`action` with `slot` and `value`), or both.
  - The actions are `choose`, `correct`, `not_sure`, `skip`, `explain`, `confirm_yes` and `confirm_no`.
  - Account scenarios (Stage 3 of the master upgrade) add `new_scenario` (a separate account; the current one is kept), `change_application` (change this account's holder instead) and `switch_scenario` (go back to an earlier account; `value` is its `index` from `scenarios`, no `slot`).
  - `value` must be one of the slot's chip values.
- **Errors** use the existing codes-only envelope `{error: {code, message}}` and never echo the request:

  | Status | Code | Meaning |
  |---|---|---|
  | 400 | `IDEMPOTENCY_KEY_REQUIRED` | The header is missing or longer than 200 characters. |
  | 404 | `SESSION_NOT_FOUND` | The session is unknown, or the id is not valid. |
  | 409 | `IDEMPOTENCY_MISMATCH` | The same key was used for a different request. |
  | 410 | `SESSION_EXPIRED` | The session has expired. |
  | 422 | `VALIDATION_ERROR` | Invalid JSON, an unknown field or value, or a body over 16 KB. |
  | 503 | `ASSISTANT_UNAVAILABLE` | The assistant service is not available. |
- **Sessions** expire 24 hours after their last stored turn.

## 2. The answer object (`AssistantAnswer`; A1 §8.1)
- **`kind`** is one of:
  - `question` (with `question`: slot, text, chips, and whether "not sure", "skip" and "explain" are offered);
  - `confirm` (with `confirmation`: slot, value, text);
  - `checklist` (with `checklist`: groups A/B/C, sources, coverage note);
  - `fallback` (with `fallback`, the A1 §7 text verbatim; a next question may ride along);
  - `cannot_build`;
  - `blocked` (screening refused the message);
  - `technical_error` (with `technical_message`, the A1 §7 text verbatim).
- **Every answer** also carries:
  - `profile`: each slot's status, value, chip label and origin, plus `choices`: the slot's valid values with their labels, for a "Change" control (added at A6, option A; additive; the server still validates every correction);
  - `messages`: dialogue notes, the blocked text, or why a checklist can't be built yet;
  - `coverage_state` when it is known;
  - **`reply`:** what the assistant says in the chat for this answer:
    - its notes and the question or confirmation, or a short checklist summary (item titles from the answer);
    - polite redirects for messages it can't use.

    It is composed deterministically (`reply.py`) and, when the live LLM is enabled, reworded by the validated voice (`voice.py`). It falls back to the deterministic reply on any failure, and is stored with the answer, so replays are identical. Whether an LLM wrote it is never exposed. There is no reply for `blocked` or `technical_error`;
  - on a `fallback` for a combination the documents don't cover:
    - **`explanation`:** why, in plain language. It comes from the coverage map's customer wording (`coverage.yaml`'s `explanation` / `default_explanation`, checked on load for internal wording). The internal `reason` is still never sent.
    - **`suggestions`:** the combinations the coverage map does cover, each as `{slot, value, label}`. The UI sends a chosen one back as a `correct` turn, which the server validates. Added after the A6 browser test; additive.
  - **`notice`**: the dated-document notice, verbatim, on every answer.
  - **`scenario_offer`** (on a `question` answer, instead of `question`): `{text, options[{action, label, value}]}` when a message may be about another account. The options send `new_scenario` / `change_application`, or `switch_scenario` / `confirm_no` to go back or stay. Nothing changes until the customer chooses.
  - **`scenarios`** and **`active_scenario`**: the other accounts parked in this conversation (`{index, label}`) and, when there are any, a label for the account being discussed. Each keeps its own answers; they are never merged.
- **Seven kinds (accepted at the A5 close-out):** A1 §8.1 originally listed four. A5 adds `confirm`, `cannot_build` and `blocked`, which are dialogue outcomes A3 already had. A1 §8.1 now lists all seven with when each is used.
- **The opening answer** (turn 0) starts with a `privacy` message: "This is an independent prototype, not an HDFC Bank service. Please don't enter real personal or banking information, …" (wording in `dialogue.yaml`).
- **Never sent** (tested):
  - citation chunk ids and A4 diagnostics;
  - the internal not-established reason and the checklist `reason` (coverage-map wording);
  - why a value is being confirmed (hint, conflict or LLM);
  - whether an LLM was consulted;
  - retrieval scores and stage names.
- **Schema names:** the published schemas carry an `Assistant` prefix (`AssistantQuestion`, `SourceCitation`, …), so the v1 schema names in `docs/api/openapi.json` are unchanged. The frontend types changed by additions only.

## 3. One turn (`app/assistant/service.py`)
1. The raw body is parsed into `TurnInput`. Any parse or validation error returns 422 without echoing the input.
2. The session row is locked (`SELECT … FOR UPDATE`). A known `Idempotency-Key` returns the stored answer, byte-identical; a different body with the same key gets 409.
3. The A3 `respond` runs. It screens the message itself. The optional LLM is used only behind the existing gate (`GROQ_LIVE_ENABLED` and the data-terms date); it is off by default, so the assistant is deterministic.
4. If the profile is ready, the A4 `ChecklistService` builds the checklist from stored HDFC chunks. This runs in a worker thread, because the code is sync.
5. The answer, the new state and the turn are stored in one transaction.

**Technical errors:** if the checklist can't be built (storage, retrieval or model loading fails), the customer gets the fixed technical message and **nothing is stored**. Sending the same request again simply retries.

## 4. Persistence (Alembic `0009_a5_assistant`; existing tables untouched)
| Table | Contents |
|---|---|
| `assistant_sessions` | `state` (the A3 `DialogueState` as JSONB: enum codes only), `turn_count`, `expires_at` |
| `assistant_turns` | One row per turn, including the opening question (turn 0). **Insert-only:** a trigger rejects UPDATE. `message` is the **screened** text, or NULL when screening refused it (only the categories are kept). `request` holds the enum-validated choice fields. `answer` is **JSON, not JSONB**, because JSONB reorders keys and replays must be byte-identical. |
| `assistant_idempotency` | (session, key) → request hash and the turn that a replay returns |

Rows are deleted only by cascade with their session. Retention is not in A5.

## 5. Session security and privacy (A5 close-out review)
- **Session ids** are `uuid.uuid4()`: 122 random bits from the operating system's cryptographically secure generator (`os.urandom`). They can't feasibly be guessed (tested: version 4, distinct).
- **Access:** a conversation can be read or continued only with its id. There is no endpoint that lists or searches conversations, and a request for one session never returns another's turns (tested). Authentication is deliberately out of scope: the id is the only handle, which is acceptable only because the prototype holds synthetic data.
- **Logs:**
  - Request bodies and customer messages are never logged. The service logs only exception class names, and a test checks that no assistant module passes a message, session id or key to the logger.
  - The server's access log (uvicorn) prints every request path. Because the session id is in the path, `app.core.logging` masks it as `/api/v2/assistant/sessions/<session>/…` (tested). The preserved v1 paths are unchanged.
- **Screening:** identifiers (PAN, Aadhaar, passport, card and account numbers) are refused and never stored; e-mail addresses and phone numbers are stored only in redacted form.
- **Real data:** the prototype is **not intended for real customer data**. The opening answer says so, the screening message repeats it when an identifier is entered, and the A6 screen keeps the persistent disclaimer (A1 §7).

## 6. Conversation retention (current behaviour)
- **Kept until deleted by hand.** Conversations, turns and idempotency keys stay in Postgres indefinitely. There is no automatic deletion job in A5.
- **Expiry only closes access.** A session expires 24 hours after its last stored turn: after that, the API answers `410 SESSION_EXPIRED` and the conversation can't be read or continued, but its rows remain.
- **What a row holds:** enum codes for the profile, the screened message text (identifiers never; e-mail addresses and phone numbers only as `[REDACTED_…]`), and the answers given.
- **Deleting by hand:** `DELETE FROM assistant_sessions WHERE …` removes a conversation; its turns and keys go by cascade. Turns can't be edited (insert-only trigger).
- **Not for real customer data:** because nothing is deleted automatically and there is no authentication, the prototype must only ever receive synthetic data. An automatic purge would be a later, separately approved stage.

## 7. Demo (development stack)
```bash
make up && make migrate && make api        # http://127.0.0.1:8000
S=http://127.0.0.1:8000/api/v2/assistant/sessions
SID=$(curl -s -X POST $S | python -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
t(){ curl -s -X POST "$S/$SID/turns" -H "Idempotency-Key: $1" -H "Content-Type: application/json" -d "$2"; }
t k1 '{"message":"I live in Singapore and want to open an NRE savings account"}'   # confirm: NRI?
t k2 '{"action":"confirm_yes"}'                                                   # question: holding
t k3 '{"action":"not_sure","slot":"holding"}'                                     # question: minor
t k4 '{"action":"not_sure","slot":"minor"}'                                       # checklist (COVERED)
t k4 '{"action":"not_sure","slot":"minor"}'                                       # replay: identical bytes
t k5 '{"message":"my PAN is ABCPE1234F"}'                                         # blocked; not stored
curl -s "$S/$SID"                                                                 # reload the conversation
```
Recorded on 2026-11-14 on the development stack:
- **The checklist:** 5 documents and 16 information items. All are "Asked for on the HDFC Bank form" and cite the NRI account-opening form with pages.
- **The replay:** the repeated `k4` request returned byte-identical content.
- **Reusing a key:** sending `k4` with a different body gave 409.
- **Screening:** the PAN turn was blocked and did not appear in the reloaded conversation.

## 8. Tests
- **`tests/unit/test_assistant_api.py`** (37 tests; in-memory store, fake stored chunks, no LLM) covers:
  - golden conversations: LLP current plus the PAN card question; NRI savings from free text; resident savings → fallback; the conversion;
  - parity with the A3 dialogue;
  - screening, idempotency, validation without echo, 404/410/503, and the technical-error path;
  - session reload, the documented contract, the JSON round trip of the state, and the notice;
  - the close-out checks: the privacy message, random distinct session ids, one session never showing another's turns, no listing endpoint, the masked access log, and no logging of messages or ids.
- **`tests/integration/test_assistant_api_dev.py`** (5 tests; a throwaway Postgres for conversations, the development corpus for evidence) covers:
  - 0009 being additive and reversible;
  - a complete NRI savings conversation with citations checked against stored text;
  - no PAN anywhere in the stored rows;
  - the insert-only trigger and cascade;
  - concurrent requests with one key storing one turn;
  - an unknown session.

## 9. Not in A5
- The chat screen (A6).
- Automatic deletion of old conversations (see §6).
- Authentication: the session id is the only handle, which is acceptable for a synthetic-data prototype.
- Any change to the dialogue policy, the catalogue, coverage or retrieval. The one addition to A3 is `dialogue.opening()`, which returns the first question without a turn; its policy is unchanged.
