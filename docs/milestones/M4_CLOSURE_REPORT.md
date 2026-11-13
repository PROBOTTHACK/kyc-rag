# M4 Closure Report: claim verification infrastructure and human-review cases

> Independent educational prototype. It is not an HDFC Bank or RBI system and not endorsed by either. It uses synthetic data only, makes no KYC approval or rejection, and every result requires human review.

## Statement
**M4 is complete.** Stages S0–S9 are finished and owner-approved. S9 is the final M4 stage: there is no S10 and no further M4 milestone. M4 delivered the claim-verification infrastructure, a pre-registered NLI evaluation, and human-review cases. Its central evidence-based outcome is that **no NLI configuration qualified (0 of 140)**, so semantic verification ships **disabled**. Every claim is reported *not verified*, never silently trusted.

## 1. Milestone status (M0–M4)
| Milestone | Status | Branch state |
|---|---|---|
| M0 Scaffold | complete | in `main` |
| M1 Document ingestion | complete | merged into `main` (`d8d8b7b`) |
| M2 Retrieval and form intelligence | complete | merged into `main` (`a288b09`) |
| M3 LangGraph workflow | complete | merged into `main` (`5c3250d`) |
| **M4 Claim verification and review** | **complete** | branch `feat/m4-s0-plan`, 14 commits on top of `main` (13 through S8, plus the S9 commit). At closure: not merged and not pushed, pending the owner's decision. **Update (2026-11-13, M5 S0):** merged into `main` (`0b65df0`) and pushed by the owner. |

## 2. M4 commit history (`main..feat/m4-s0-plan`)
| Commit | Stage |
|---|---|
| `b95c102` | S0: locked plan, NLI model metadata, docs aligned |
| `c12cfb3` | S1: claim and verification models, report v2 with v1 compatibility |
| `f8ba9f7` | S2: deterministic verification foundation |
| `674b14c` | S3: golden claim datasets, labelling guidelines, held-out freeze |
| `3a82fea` | S3: owner-reviewed claim datasets finalized |
| `f47e331` | S4.1: NLI scorer, dev evaluation harness, pre-registered selection rule |
| `0c4cc49` | fix: selection CLI paths resolve against the repo root |
| `91fcac1` | S4.2: registered NLI selection run; 0 of 140 qualified |
| `7093102` | S5: `claim_verification` node, NLI disabled by default |
| `e8d6d2d` | S5 follow-up: `check_report` validates the full report |
| `2b09d72` | S6: bounded source-conflict scan and supersession rule |
| `e38530c` | S7: open review cases, read endpoint, retention |
| `ff666fa` | S8: held-out frozen and unused, M4 acceptance rules, real-corpus end to end, latency |
| (this commit) | S9: final handoff, evaluation report, known-issues register, README, closure |

## 3. Delivered vs future work
**Delivered in M4:**
- **Models:** typed atomic claims; report v2 (v1 reports unchanged and still valid).
- **Verification pipeline:** premises from stored text, deterministic statement analysis, and a decision table that only ever lowers applicability.
- **Datasets:** an owner-reviewed development claim set (55); a frozen held-out claim set (51, unused).
- **Local NLI scorer** with a pinned revision, label map from config, a sanity triplet and no truncation. Also a registered selection protocol and its write-once record.
- **The `claim_verification` graph node**, with an honest NLI-disabled mode.
- **Bounded source-conflict scan** and the D10 supersession rule.
- **Open, insert-only review cases** (migration 0008) and their read-only endpoint; retention and integrity checks.
- **M4 acceptance rules** for every verification state.
- **Real-corpus end-to-end tests** and a latency measurement.
- **The final handoff:** `docs/handoff/01`–`04` and this report.

**Future work (outside M4, each needing separate owner approval):**
- a new, versioned NLI protocol that fixes the deferred S2 defects before any re-selection. The held-out set is preserved for it.
- reviewer decisions with authentication; Prompt Guard and an adversarial suite (future security milestone, number TBD);
- a decision on the citation page-range mismatch (K5);
- frontend (M5), benchmark and observability (M6), deployment.

## 4. Final audit (S9)
| Check | Result |
|---|---|
| Unit (`make test`) | 1,492 passed, 1 failed (known: K7), 510 deselected |
| Integration (full suite) | 504 passed, 2 failed (known K8: `test_dense_golden`, `test_hybrid_dev`), 4 deselected |
| `make lint` (ruff, black, strict mypy on 124 source files) | clean |
| `make check-schema` | clean ("No new upgrade operations detected") |
| `make check-workflow` | `"ok": true`, no review-case problems |
| `git diff --check` | clean |
| Secret/configuration scan of tracked files | no tracked `.env`. No credential-like strings except one deliberately fake test literal (`AIzaTEST-not-a-real-key-…` in `test_gemini_provider.py`). `infra/.env.example` has empty keys and every live gate, the cache, the reranker and tracing off, and `NLI_MODEL` empty. |
| Held-out SHA-256 | `eee0a575319d7df4bc3d8f5c0771a89e4c8bf9f68abe36eb56fcac3275d90203` (unchanged; bytes hashed, never parsed for scoring) |
| S4 record SHA-256 | `f80f8b3f3d4a5494a2b00529d96aba2f05336d8414682e94cfd68770bdcdebb4` (unchanged since `91fcac1`) |
| `data/` | identical to `ff666fa`; S9 changed documentation only |
| Working tree | clean after the S9 commit |

**The suite is not entirely green.** Three known baseline failures remain, all documented and none caused by M4 code changes: K7 (the Groq environment-dependent unit test) and K8 (two Qdrant tied-score integration tests).

## 5. Outstanding known issues
See [`docs/handoff/04-limitations-and-known-issues.md`](../handoff/04-limitations-and-known-issues.md):
- **K1–K4:** deferred S2 verification-rule defects;
- **K5:** the trust-run citation/page mismatch;
- **K6:** the legacy `fallback_text_missing` wording;
- **K7–K8:** environment-dependent test failures;
- **K9–K10:** the S4 determinism tolerance and N4's CPU cost;
- **K11:** six held-out annotation issues;
- **K12:** the single-annotator dev set.

## 6. Handoff documents
- [`docs/handoff/01-architecture-handoff.md`](../handoff/01-architecture-handoff.md): M0–M4 history, architecture as built, feature status.
- [`docs/handoff/02-setup-and-reproducibility.md`](../handoff/02-setup-and-reproducibility.md): clean-checkout setup, commands, troubleshooting.
- [`docs/handoff/03-evaluation-report.md`](../handoff/03-evaluation-report.md): M1–M4 results, the S4 selection, ablation, latency, the held-out decision, final counts.
- [`docs/handoff/04-limitations-and-known-issues.md`](../handoff/04-limitations-and-known-issues.md): limitations, safety, privacy, known issues.
- [`M4_PLAN.md`](M4_PLAN.md): the locked plan and the per-stage outcomes S0–S9.

## 7. Owner actions after M4
1. Review this closure, then decide whether to merge `feat/m4-s0-plan` into `main` and push. Neither was done in M4. **Done after closure:** merged as `0b65df0` and pushed to `origin/main` (recorded in M5 S0).
2. Optionally, run the test suite in an environment without `GROQ_DATA_TERMS_VERIFIED_ON`, to confirm K7 is environmental.
