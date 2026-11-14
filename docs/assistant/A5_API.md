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
  - `profile`: each slot's status, value, chip label and origin;
  - `messages`: dialogue notes, the blocked text, or why a checklist can't be built yet;
  - `coverage_state` when it is known;
  - **`notice`**: the dated-document notice, verbatim, on every answer.
- **Extension of A1 §8.1, for review:** §8.1 lists four kinds. A5 adds `confirm`, `cannot_build` and `blocked`, which are dialogue outcomes A3 already had.
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

## 5. Demo (development stack)
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

## 6. Tests
- **`tests/unit/test_assistant_api.py`** (32 tests; in-memory store, fake stored chunks, no LLM) covers:
  - golden conversations: LLP current plus the PAN card question; NRI savings from free text; resident savings → fallback; the conversion;
  - parity with the A3 dialogue;
  - screening, idempotency, validation without echo, 404/410/503, and the technical-error path;
  - session reload, the documented contract, the JSON round trip of the state, and the notice.
- **`tests/integration/test_assistant_api_dev.py`** (5 tests; a throwaway Postgres for conversations, the development corpus for evidence) covers:
  - 0009 being additive and reversible;
  - a complete NRI savings conversation with citations checked against stored text;
  - no PAN anywhere in the stored rows;
  - the insert-only trigger and cascade;
  - concurrent requests with one key storing one turn;
  - an unknown session.

## 7. Not in A5
- The chat screen (A6).
- Retention or purging of old sessions.
- Authentication: the session id is the only handle, which is acceptable for a synthetic-data prototype.
- Any change to the dialogue policy, the catalogue, coverage or retrieval. The one addition to A3 is `dialogue.opening()`, which returns the first question without a turn; its policy is unchanged.
