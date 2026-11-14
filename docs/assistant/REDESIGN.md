# Product redesign: the Account Opening Assistant as the application

Stage: the product redesign and consolidation before A7 (owner request, 2026-11-14), on `feat/account-opening-assistant`. The Account Opening Assistant is now the main application, with a dark design system and a rebuilt journey. Its working behaviour is unchanged: the same A5 API, dialogue, checklist, idempotency, session recovery, corrections and PII safeguards.

## 1. Application structure
| Route | What it is |
|---|---|
| **`/`** | **The Account Opening Assistant** (`frontend/src/assistant/`). |
| `/open-account` | Redirects to `/` (the A6 address keeps working). |
| `/legacy`, `/legacy/assistant`, `/legacy/reports/:id` | The preserved M5 compliance-engine screens, unchanged apart from their address. They are not linked from the assistant. |
| anything else | The assistant's own "Page not found", linking to `/`. |

**Shell:** the assistant has its own:
- **header:** the product mark (an abstract "verified document" glyph; no third-party branding), its name, "Independent prototype" and **Start again**;
- **persistent disclaimer ribbon:** all A1 §7 claims.

The phase (welcome, then the workspace) is state, not part of the URL, so the session id never reaches the address bar.

## 2. The journey
1. **Welcome.** The headline, what the assistant does and three principles (evidence not guesses; dated sources clearly marked; no approvals or scoring). Also:
   - the **real-data warning before any input** (there is no text box until Start);
   - **I understand — start**;
   - a decorative schematic of the result: bars, not invented content.
2. **Account discovery.** The API's question appears as large selection cards with icons, in the API's order, with only the API's choices. "I'm not sure", "Skip" and "What does this mean?" appear only when the API offers them; free text is always available.
   The account-type list is the dialogue engine's own. Combinations the HDFC documents don't cover are answered with the prescribed fallback after selection, so the frontend never filters or adds choices.
3. **Workspace.**
   - **Wide screens (≥ 1280 px):** a rail (progress and your answers), the conversation, and the checklist as its own column.
   - **Laptop and tablet (1024 px and up):** the rail, plus a **Conversation / Checklist** switch.
   - **Mobile:** a compact progress bar, a **Your answers** button that opens a bottom sheet, and the switch. The first checklist takes the stage automatically.
   - **Message entrance:** messages appear with a short entrance motion.
   - **Answer dock:** the question cards or Yes / No sit between the transcript and the composer.
   - **Errors:** failures show inline with **Try again**, which resends with the same Idempotency-Key.
4. **Your answers.** Confirmed answers have a green dot and say how they were given. Answers marked "Not sure" or "Skipped" have an amber dot and are shown in italics. Each answer keeps its **Change** control, which offers the API's choices for that slot.
5. **Checklist (the centrepiece).** A document workspace:
   - **Summary:** item counts per group, the coverage note, **Expand all** and **Print checklist**.
   - **Groups:** **A** documents and declarations, **B** information to provide, **C** could not be confirmed (only when the API sends it).
   - **Each item, collapsed**, shows:
     - the kind icon and title;
     - the classification label and kind (Document / Declaration / Information);
     - "Applies if…" notes;
     - a **Scanned form** flag;
     - the condition;
     - the source line (*document · page*).
   - **Expanded**, it adds:
     - "The form says" (an exact quotation, for reliable text only) **or** "Approximate reading of the scanned form" with the OCR warning, never in quotation marks;
     - **Why this is here** (what the form establishes and what it does not);
     - caveats;
     - **source disclosures**.
   - **Group C items** always show their plain explanation and the "Could not be confirmed" label, never "Asked for on the HDFC Bank form".
6. **Sources and evidence.** Each source panel shows the document, page or pages, section, document date, and whether the text comes from the document or is an automatic reading of a scanned page. It also shows the stored passage. Related form fields are labelled as context only. A summary lists the documents the checklist uses, with their dates and a scanned flag. No database ids, document keys, scores or diagnostics are shown (tested).
7. **Print.** A dedicated print stylesheet:
   - white page and dark text; light colour scheme;
   - the conversation, rail and controls are hidden;
   - every item's evidence is expanded;
   - the disclaimer ribbon and a print-only product and disclaimer heading are kept, as is the dated-document notice.

   There is no PDF infrastructure; the browser prints.

## 2a. A fallback that explains and suggests (owner decision after the browser test)
**The problem:** a combination the documents don't cover (resident savings, LLP savings, salary) used to end in the bare fallback. The answer now has three parts.
- **The prescribed fallback text**, verbatim, as before.
- **A plain explanation** from the coverage map's customer wording, for example: "The HDFC Bank form available to this assistant says savings accounts are for individuals, so it doesn't cover a savings account for an organisation."
- **"Covered by the documents" cards**, read from the coverage map:

  | When | The cards offer |
  |---|---|
  | The customer category is known | the account types covered for that category (LLP → Current, Fixed / term deposit, Recurring deposit) |
  | Nothing is covered for that category | the categories covered for the chosen account (resident savings → an individual living outside India) |
  | Only the account type is known (salary) | every account type the documents cover |

  A card sends an ordinary `correct` turn, which the server validates.

Coverage decisions are unchanged, and the internal reason is still never shown. Verified in the browser by replaying the reported conversation: LLP + Savings shows the explanation and three cards, and choosing "Current account" builds the LLP checklist (screenshot `17_fallback_explain_suggest.png`).

## 2b. A conversational voice (owner decision after the second browser test)
**The problem:** after a checklist existed, every unreadable message ("bleeh", a prompt-injection attempt) got "Your checklist has been updated." with nothing changed, and questions got canned lines.

**The fixes:**
- **Deterministic:**
  - an unreadable message after the checklist is out of scope: the prescribed fallback plus a polite redirect;
  - every answer has a `reply`: its notes and question, or a checklist summary ("Here's what the HDFC Bank form asks for …: 4 documents to bring and 14 details to fill in", followed by the documents as bullets, then a pointer to the sources). The wording comes from `dialogue.yaml`; titles, labels and numbers come from the answer.
- **The LLM voice:** with Groq enabled, the reply is reworded warmly from the answer's facts only. It is checked deterministically (numbers and document terms must be in the facts; no obligation or approval wording; no "only covers" claims; no internal wording, links or code). The deterministic reply is used on any failure.
  - **Calls:** chip clicks on questions don't call the LLM (fast); free text, confirmations, checklists and fallbacks do.
- **Live check (2026-11-14):** the owner's conversation was replayed at a human pace with `openai/gpt-oss-120b`.
  - **Replies:** natural questions, a friendly checklist summary, polite redirects for the injection and "bleeh". The validator rejected a reply that tried to mention a passport "copy", and the safe reply was used.
  - **Limits:** a few calls timed out because of the free-tier token budget the app enforces (`RATE_LIMIT_GROQ_TPM=6000`); those turns used the deterministic reply.

## 2c. Context awareness and a simpler workspace (owner request, before A7)
**Context.** Each message is read against the conversation (A3 addendum, `A3_DIALOGUE_POLICY.md`):
- a child or person mentioned against an organisation profile is explained and clarified once, with nothing changed until the customer chooses;
- a follow-up keeps the profile and gets a brief answer;
- a separate request keeps the checklist and offers a separate one;
- an answer that doesn't apply to the account is never stored.

The LLM voice reads the last three turns only to resolve references, and is told the turn's situation (follow-up, clarify, separate request, off-topic, not covered).

**Workspace.** It is built around one conversation column on every screen size:
- a compact progress line;
- a **Conversation / Checklist** switch once a checklist applies (it no longer opens by itself: the chat shows the summary and **Open checklist**);
- **Your answers** in a drawer: a right-hand panel on desktop, a bottom sheet on mobile. It holds the progress steps, the answers with Change, and the notice.

The checklist built for the current answers stays available during follow-ups and clarifications, and disappears once an answer changes.

**Less noise:** no permanent side rail, no stat tiles, glow or decorative eyebrow labels in the checklist; plain small labels; compact cards for long choice lists; and the notice at the foot of the conversation.

The checklist evidence features are unchanged: groups A/B/C, conditions, sources with pages, OCR warnings, expand/collapse, print.

**Screenshots:**
- `18_context_clarify.png`
- `19_mobile_simplified.png`
- `20_mobile_checklist_view.png`

## 3. Design system (`frontend/src/index.css`, Tailwind 4 `@theme`)
- **Colours:**

  | Token | Value |
  |---|---|
  | `void` (background) | `#090D16` |
  | `surface` | `#111827` |
  | `raised` | `#172234` |
  | `hover` | `#1D2A3F` |
  | `line` | `#263449` |
  | `line-strong` | `#344760` |
  | `fg` | `#F3F7FC` |
  | `muted` | `#A4B1C4` |
  | `subtle` | `#7D8CA3` |
  | `cyan` (primary accent) | `#54D6FF` |
  | `violet` (secondary accent) | `#9275FF` |
  | `ok` | `#4FD1A1` |
  | `warn` | `#F0B95A` |
  | `danger` | `#F07878` |

  Group accents: A is cyan, B violet, C amber.
- **Type:** `font-display` (Space Grotesk) for headings, `font-ui` (Inter) for the interface, `font-code` (JetBrains Mono) for metadata, numbers and small labels. They load from Google Fonts (`index.html`, `display=swap`) with system fallbacks. **No npm dependency was added**; the font request goes to Google's servers.
- **Surfaces:** layered graphite and navy surfaces with fine 1px borders. Light is used sparingly: one faint grid and two soft glows on the welcome screen, the primary button's halo, and a header glow on the checklist.
- **Icons:** one inline SVG set (`assistant/ui.tsx`) with 1.6px strokes on a 24px grid. Choice icons are decoration only, and an unknown value gets a neutral icon.
- **Motion:** 150–300 ms entrance, fade and accordion transitions, the "thinking" dots, and progress transitions. **All are off under `prefers-reduced-motion`.**
- **Focus:** a 2px cyan outline on every keyboard focus. It sits in the base layer, so components can refine it; programmatic focus targets have none.
- **The legacy screens keep Tailwind's default palette** and are unaffected.

## 4. Consolidation audit (frontend only; no backend, migration, document or test was deleted)
| Item | Decision | Why |
|---|---|---|
| `src/assistant/*` | **Redesign** | The product. `ConversationScreen.tsx` (A6) is replaced by `Workspace.tsx`. |
| `useConversation`, `api/client.ts`, `api/types.ts`, `api/errors.ts`, `api/idempotency.ts`, `storage/assistantSession.ts` | **Keep** | The A5 integration, idempotency and recovery, unchanged. |
| `components/ErrorBoundary`, `components/useFocusWhenLost`, `api/hooks` (`ApiContext`) | **Keep** | Shared infrastructure used by the assistant. |
| `screens/dashboard`, `screens/assistant` (v1), `screens/report`, `screens/NotFoundPage`, `components/Layout`, `components/Disclaimer` | **Modify (moved to `/legacy`)** | The preserved M5 compliance engine. It still works and its tests pass, but it is no longer in the assistant's path. |
| `api/events.ts`, `api/sse.ts`, `storage/sessions.ts`, `test/fixtures/live`, `test/fixtures/reports` | **Keep** | Used only by the legacy screens. |
| `screens/Placeholder.tsx` | **Proposed removal, needs approval** | It has no importers anywhere. |
| The legacy screens themselves | **Proposed removal, needs approval** | Possible later. CLAUDE.md says legacy removal happens only in a reviewed, owner-approved stage, so they are kept for now. |

## 5. Verification (2026-11-14)
- **Frontend:** 175 tests in 19 files pass. ESLint, Prettier, `tsc` strict and the production build pass, and `check:api` reports the generated types as current.
  - **Assistant tests:** the redesign changed some assertions. Card text has no "→" (the arrow is an icon), sources are inside collapsed details, and focus moves on the next frame. New tests cover:
    - the summary versus the hidden evidence;
    - Expand all / Collapse all;
    - Group C visible and never labelled as a requirement;
    - the scanned flag;
    - print;
    - the answers sheet (dialog, Escape, focus return, corrections);
    - the Conversation / Checklist switch;
    - `/` and `/open-account`, and the assistant's not-found page.
  - **Legacy tests:** only their addresses changed (`/legacy…`).
- **Browser:** headless Chrome 157 against the real backend (development corpus, no LLM), at 1440, 1024, 768 and 390 px:

  | Check | Result |
  |---|---|
  | Fonts loaded | yes |
  | Text box before Start | none |
  | Keyboard focus | 2px cyan outline |
  | NRI savings checklist | reached, 20 items |
  | Session id in the URL or page | absent |
  | Expanding an item and its source | works |
  | Print preview | white background; conversation hidden; **12 of 12 collapsed details expanded for paper** |
  | Follow-up question | checklist updated |
  | Synthetic PAN | blocked and never on the page (also after reload) |
  | Reload | 15 bubbles restored |
  | Change of account type | sent as a correction; checklist updated |
  | 1024 px | rail and switch |
  | Mobile LLP current account | the checklist took the stage; Group C after the PAN-card question; the answers sheet works |
  | 768 px resident savings | the fixed fallback |
  | Horizontal overflow | none at any width |
  | Console | **no errors or warnings** |
- **Bugs found in review and fixed before the commit:**
  - checklist cards were clipped by a grid `min-width`;
  - the focus style overrode component styles because it was unlayered;
  - print rendered dark text on a dark canvas (`color-scheme`);
  - print kept details collapsed (Tailwind's `[hidden] !important` in its base layer);
  - "Start again" wrapped on mobile.

**Screenshots** are in `docs/assistant/redesign/`:

| File | Shows |
|---|---|
| `01_welcome_desktop` | Welcome screen, desktop |
| `03_discovery_desktop` | Account discovery cards, desktop |
| `05_checklist_wide` | Checklist workspace, wide screen |
| `06_item_source_wide` | An expanded item and its source, wide screen |
| `07_print` | Print preview |
| `08_blocked_wide` | A blocked message, wide screen |
| `10_laptop_1024` | Laptop layout at 1024 px |
| `11_welcome_mobile` | Welcome screen, mobile |
| `13_checklist_mobile` | Checklist, mobile |
| `14_groupc_mobile` | Group C, mobile |
| `15_sheet_mobile` | The answers sheet, mobile |
| `16_fallback_tablet` | The fallback, tablet |

## 6. Known issues and notes
- **Account-type cards:** they include choices such as "Salary account". The dialogue engine offers them, and the documents then answer with the prescribed fallback; the frontend doesn't filter the API's choices. Hiding unsupported choices before selection would need an A3/A5 change (not done).
- **Fonts:** they load from Google Fonts at runtime. Self-hosting them would need an npm dependency (`@fontsource/*`) and owner approval.
- **Port 8000:** an old pre-A5 API process was still listening on port 8000 during this work, so the browser review ran the backend on port 8001 (see A6_UI.md §7). `make api` / `make web` need that old process stopped.
- **Legacy screens:** they keep the light M5 styling. They're reachable only by typing `/legacy`.
