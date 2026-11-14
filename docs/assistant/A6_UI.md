# A6: Customer-facing account opening assistant (`/open-account`)

> **Updated by the product redesign (2026-11-14; [`REDESIGN.md`](REDESIGN.md)).** The assistant is now the application at **`/`**, and `/open-account` redirects there. It has a dark design system and a rebuilt workspace and checklist. The legacy M5 screens moved to `/legacy`. The behaviour described below is unchanged; the layout and visual descriptions in §2 and §5 are superseded by REDESIGN.md.

Stage A6 of the HDFC Bank Account Opening Assistant (independent portfolio prototype; synthetic data only). It is the web interface for the A5 API (`/api/v2/assistant`). The frontend decides nothing about requirements: every question, checklist item, classification, condition, quotation, page and source it shows comes from the API answer.

## 1. Owner decisions (A6 plan review, 2026-11-14)
- **Placement:** a new route **`/open-account`** with its own customer-facing shell. The legacy dashboard, navigation, routes and screens (M5) are unchanged.
- **Corrections (option A):** a small, additive A5 change. Every profile slot in an answer now carries `choices`, the slot's valid values with the same labels as its question. They come from the existing wording file (`dialogue.yaml`), which is checked against the allowed values. The server still validates every `correct` turn: an invalid value gets `422 VALIDATION_ERROR`. No account types, customer categories or dialogue rules were added.

## 2. Screens and components (`frontend/src/assistant/`)
| Component | Role |
|---|---|
| `AssistantApp` | The shell: header (product name, "Independent prototype", **Start again**), the persistent disclaimer, a skip link, one screen at a time. |
| `WelcomeScreen` | Purpose, what it can and cannot do, the **real-data warning**, and **"I understand — start"**. Nothing can be typed before Start. It also holds `EndedScreen`, shown for an expired or unknown conversation. |
| `ConversationScreen` | The layout. **Desktop:** the conversation (left) beside progress and your answers (right), with the checklist below the conversation. **Mobile:** progress, then the conversation, then your answers, then the checklist. |
| `Transcript` | Customer and assistant bubbles. The privacy message is shown as a warning card. A blocked message reads only "Message not sent". |
| `AnswerPanel` | `QuestionCard`: large choice cards plus "I'm not sure", "Skip" and "What does this mean?", each only when the API offers it. `ConfirmCard`: Yes / No. |
| `Composer` | Free text, with the real-data reminder above it. Enter sends and Shift + Enter adds a new line; the box is cleared on send; 2 000 characters at most. |
| `StatusNotice` | A turn without an answer: the API's fixed technical message, or a fixed text for an error code. **Try again** appears only when the same turn can safely be resent. |
| `ProfilePanel` | **Progress steps** (derived only from the answer: which answers are known and the answer kind) and **Your answers**, each answer with how it was given and a **Change** control offering the slot's `choices`. |
| `ChecklistView` | Groups A (documents and declarations), B (information) and C (could not be confirmed, **only when the API sends entries**). Each item card shows the following, all from the API:<ul><li>the title, a kind badge and the classification label;</li><li>"Applies if…" notes and the condition;</li><li>the exact quotation, for reliable text only, **or** an approximate reading of a scanned form with the OCR warning, never in quotation marks;</li><li>what the item does and doesn't establish, the note and caveats;</li><li>expandable **sources** (document, page or pages, section, document date, stored passage), with scanned passages labelled as automatic readings;</li><li>related form fields (context only), labelled as such.</li></ul>The checklist also shows the coverage note, the documents used (dates, scanned flag), the dated-document notice and **Print checklist**. |
| `useConversation` | The only place that talks to the API: start, reload, turns, retries and the session id. |
| `labels.ts` | Interface wording only: headings, buttons, slot names and error texts. No requirement text. |

**All seven answer kinds** are handled:
- **`question`:** choice cards.
- **`confirm`:** Yes / No.
- **`checklist`:** the bubble says "Your checklist is ready." (later answers say "…has been updated.") with **View your checklist**, and the structured checklist renders.
- **`fallback`:** the fixed text, with any next question still offered. On a combination the documents don't cover, progress marks the last two steps "Not available for these answers".
- **`cannot_build`:** the API's reason. The customer can change an answer in Your answers.
- **`blocked`:** the fixed message. The message isn't shown back, and the earlier question stays answerable.
- **`technical_error`:** a notice with **Try again**.

## 3. API integration
- **Client:** `api/client.ts` gains `assistantStart()`, `assistantGet(id)` and `assistantTurn(id, body, key)` on `/api/v2/assistant/sessions`. They use the existing timeout, code-only `ApiError`, and bounded retries (transient failures only, same key). Starting a conversation is never retried automatically. `/api/v1` behaviour is unchanged.
- **Types** are aliases of the generated contract (`api/types.ts`), so nothing is redeclared.
- **What the frontend sends:** a body is `{message}`, or `{action, slot, value}` built only from the cards and choices the API sent. The server validates everything.

## 4. State, idempotency and recovery
- **Session id:** kept in memory and in **this tab's `sessionStorage`** (`storage/assistantSession.ts`, M5-D4 pattern). It is validated as a UUID, never placed in the URL, the page text or a log, and forgotten on **Start again**.
- **Reload:** the stored id plus `GET /sessions/{id}` rebuilds the transcript, your answers and the latest answer. Choice turns are labelled from the API's choices; blocked turns show "Message not sent". A 404 or 410 clears the id and shows "This conversation has ended" with **Start a new conversation**. A network failure keeps the id and offers **Try again**.
- **Idempotency:** every turn carries a key. The key is **reused** when the same turn is resent after an unknown outcome (no response, 5xx) or a `technical_error` (nothing was stored). Otherwise the key is retired.
- **One turn at a time:** cards, Change and Send are disabled while a turn is in flight, with a "Checking…" indicator.

## 5. Accessibility and responsive design
- **Structure:** landmarks, a skip link and headings in order. The question and confirmation cards are labelled groups whose name is the question.
- **Announcements:** a polite live region announces each new answer's text; errors use `role="alert"`.
- **Focus:** when the control the customer used disappears, focus moves to the new question, the confirmation, the checklist heading, or the ended screen (`useFocusWhenLost`). **View your checklist** moves focus to the checklist.
- **Disclosures and changes:** source toggles use `aria-expanded` and `aria-controls`; Change controls name their slot; the current choice is marked `aria-current`.
- **Progress:** steps use `aria-current="step"`, with status text for screen readers and visible text for unavailable steps.
- **Visual:** visible focus outlines; tap targets of at least 44 px; text on every badge, so no meaning is carried by colour alone; `motion-reduce` respected.
- **Mobile:** recorded at 390 px with no horizontal scroll. Progress becomes a compact bar.
- **Branding:** neutral navy, slate and white with one sky accent, and a generic document icon. No HDFC logo, brand colours or anything imitating the bank's own site.

## 6. Tests (Vitest; `make web-test`)
| File | What it covers |
|---|---|
| `api/assistantClient.test.ts` | v2 paths, methods, the key header, a retry with the same key, no retry on start, code-only errors, path encoding |
| `storage/assistantSession.test.ts` | `sessionStorage` only, validation, broken storage |
| `assistant/AssistantApp.test.tsx` | The welcome screen and warning before input; start; choice cards; free text; confirm; not sure, skip and explain only when offered; **blocked never echoed**; `cannot_build`; out-of-scope fallback; the notice; **technical-error retry with the same key**; network retry with the same key and a new key for a new action; no retry on 422; 410 ending the conversation; one turn at a time; reload, 404 and 410 on reload, and a transient reload failure; Start again |
| `assistant/ProfilePanel.test.tsx` | Progress derivation, `aria-current`, unavailable steps on a fallback, answers with their origin, **Change using the API's choices** to send `correct`, cancel |
| `assistant/ChecklistView.test.tsx` | Every item once, in the API's order and group; no internal ids, document keys or scores; quotation plus source, page, date and passage; condition note and applies-if; **scanned text never quoted**, with the OCR warning; **Group C only when sent**, without citations; numbered sources; coverage note, sources and notice; the checklist in the conversation |

**Fixtures:** `test/fixtures/assistant/*.json` were **recorded from the real A5 API** (development corpus, no LLM, synthetic inputs) on 2026-11-14, with one stable fake session id. Two shapes are derived and labelled as such: a `technical_error` answer and a 410.

**Backend (option A):** `tests/unit/test_assistant_api.py` checks that every profile slot offers exactly its valid choices with the question's labels, and that a correction uses a listed choice while an invalid value is still rejected.

## 7. Browser demo (real backend, 2026-11-14)
**Setup:** headless Chrome (157) driven over the DevTools protocol by a throwaway script, against the Vite dev server and the real FastAPI backend (development database, dense retriever, no LLM). Port 8000 was held by a stale process, so the demo backend ran on 8001 and the dev server proxied `/api` to it through Vite's API. **No project file was changed for the demo.**

| Step | Result |
|---|---|
| Welcome (1366 px) | Disclaimer and real-data warning visible; **no text box before Start** |
| NRI savings from free text | A confirmation, then NRE, then "not sure" for holders, then "No" for a minor → a checklist of **5 documents and 15 information items** citing the NRI form with pages (20 item cards) |
| Expand a source | Document, page, section, date and stored passage shown |
| "Do I need a photograph?" | "Your checklist has been updated." |
| A synthetic PAN | **Blocked**: "Message not sent" and the fixed text; the PAN appears nowhere on the page, before or after reload |
| Reload | The conversation was rebuilt from `sessionStorage` and `GET` (15 bubbles) |
| Session id | Not in the URL; not in the page |
| Change account type to Current (Your answers) | "I've updated your answer…" and the checklist updated |
| Mobile (390 px), LLP current account | Scanned-form items shown as approximate readings with the OCR warning, no quotation marks. After "Do I need a copy of the PAN card?", **Group C** appeared with the plain explanation and the related form fields as context only. **No horizontal overflow.** |
| Resident savings | The fixed fallback; progress marks the last two steps as not available |
| Console | **No errors or warnings** |

**Screenshots** in `docs/assistant/a6/`:
- `01-welcome.png`
- `02-conversation.png`
- `03-checklist.png`
- `04-source.png`
- `05-blocked.png`
- `06-mobile-llp.png`
- `07-fallback.png`

**Run it yourself:** `make up && make migrate && make api`, then `make web`, and open <http://127.0.0.1:5173/open-account>.

## 8. Not in A6
- Authentication.
- Automatic deletion of conversations (A5 §6 retention).
- A live LLM by default.
- New documents, coverage or dialogue rules.
- Any change to the legacy M5 screens.
