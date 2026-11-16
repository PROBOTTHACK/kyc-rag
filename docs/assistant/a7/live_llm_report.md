# A7: live LLM evaluation (E11)

Run with `make a7-live` on 2026-11-16T14:13:39+00:00 at commit `9d6976e` (with uncommitted changes); coverage `a2-2`, catalogue `a4-2`. Development stack, throwaway conversation database. **Live Groq, opt-in by the owner (2026-11-16); synthetic data only** (the committed §2.3 conversations and §2.5 PII set). Models: openai/gpt-oss-120b. No LLM cache. Local tracing only (no LangSmith upload: no key configured).

## Budget and run

| Item | Value |
|---|---|
| Dry run (no network) | 241 turns + 51 PII messages, 188 first-attempt calls |
| Budget (dry reservation x1.2), printed before the live run | 572718 tokens |
| Reserved by the live run (every attempt) | 522991 tokens, 206 attempts |
| Provider-reported usage | 187424 input + 47935 output tokens |
| Rate limits | 6000 TPM, 20 RPM; 10 s voice timeout; at most 2 retries |
| Completed | yes |
| Turns run | 241 conversation turns, 51 PII messages |
| Wall time | 181.8 min |

## Metrics (A7_PLAN §3)

| Metric | Result | Threshold |
|---|---|---|
| Live voice validity (accepted by `check_reply` / replies received) | 116/119 = 97% | >= 80 % (soft) |
| Voice fallback rate (deterministic reply used / voice invocations) | 43/159 | reported |
| Accepted replies flagged by the stricter offline audit | 55 of 116 (classified below) | 0 confirmed violations (hard) |
| Replies consistent with the stored answer | 159/159 | all |
| Turn time with the live voice, p50 / p95 / max | 2.2 / 10.3 / 10.9 s | p95 <= 12 s |
| LLM proposals accepted by the extraction filter | 0 | reported |
| Proposals committed without a confirmation | 0 | 0 (hard) |
| Synthetic identifiers in any LLM request | 0 | 0 (hard) |
| PII messages blocked (expected 29) / with an LLM call | 29 / 0 | blocked: 0 calls |
| Identifier echoed in a PII answer | 0 | 0 |
| Scripted expectations missed with the live LLM (not a gate) | 1 of 241 turns | reported |

### Fallback reasons (voice)

- cancelled: 40
- rejected: approval or obligation wording: 3

### Local tracing (capturing client; nothing uploaded)

- spans: assistant.turn 286, checklist 97, context_assembly 286, evidence_validation 97, history_retrieval 286, intent_resolution 286, llm 188, retrieval 97, voice 183
- `llm` spans carrying provider token counts: 148

## Offline re-check with the current `check_reply`

The validator was fixed after this run (A7.6: obligation wording such as "you'll need" / "requirement", and profile words not in the facts; owner decision 2026-11-16). The recorded replies were re-checked offline with the fixed check; no second live run was made (budget).

| Metric | Result | Threshold |
|---|---|---|
| Voice validity with the fixed check | 66/119 = 55% | >= 80 % (soft) |
| Accepted replies still flagged by the stricter audit | 5 | classified below |

- `follow-06#3`: bullet not traceable to the facts: '- Form\u202f60/61 (for existing customers) – each item shows the form and page it com'
- `amb-01#3`: obligation/approval wording: 'needed'
- `amb-07#2`: obligation/approval wording: 'needed'
- `amb-08#2`: obligation/approval wording: 'need'
- `off-07#3`: obligation/approval wording: 'needed'

## Manual classification of the audit flags (2026-11-16)

Every sentence flagged by the stricter audit was read.

**In the live run (55 of the 116 replies `check_reply` accepted):**

| Class | Replies | Verdict |
|---|---|---|
| "you'll need to bring one document and fill in four details", "the only document we need is Form 60/61" | about 45 | **Violation:** obligation wording. The form *asks for* items, and saying the customer needs them strengthens the classification (spec §2.5 and §8). Owner decision 2026-11-16. |
| "those requirements", "isn't listed as a confirmed requirement" | 2 | **Violation:** the same. |
| "a joint account for a company works the same way" | 1 | **Violation:** a profile claim not in the facts. |
| "documents and information needed to open an account" | 3 | Benign: the deterministic out-of-scope wording itself. |
| "I need to know who the account is for", "let me know if you need anything else", "you mentioned a minor" | 3 | Benign: no claim about items. |
| A bullet with a narrow no-break space in "Form 60/61" | 1 | Audit artifact: the bullet is in the facts. |

**Defect:** `check_reply` let these violations through. It is fixed, with regression tests in `test_assistant_voice.py` (the exact live sentences). The fix:
- every forbidden match is now checked, not only the first;
- "need" / "requirement" obligation wording is rejected;
- the profile words (joint, minor, NRE, NRO, FCNR, RFC) must come from the facts;
- the voice prompt v2 says what the form asks for.

**With the fixed check:** 66 of 119 replies are accepted. The 5 still flagged are benign, as above, so there are **0 confirmed violations**. The voice-validity soft target (80 %) is **not met** with the fixed check (55 %). Every rejected reply falls back to the deterministic reply, so the customer always gets a correct answer.

A second live run with prompt v2 was not made: it would exceed the run's budget and likely Groq's free daily quota. The prompt v2 effect on validity is therefore **unmeasured**.

**Timeouts:** 40 voice calls (25 %) hit the 10 s voice timeout and fell back to the deterministic reply. This is the gpt-oss reasoning latency on the free tier.
