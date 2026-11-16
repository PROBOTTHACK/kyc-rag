# A7.5: known failures K7, K7b and K8

Investigated on 2026-11-16 on the development stack (A7_PLAN.md §9). Both outcomes were decided by the owner. No assertion was loosened and no test was skipped or deleted.

| Id | Test | Root cause (confirmed) | Decision and fix | Commit |
|---|---|---|---|---|
| K7 | `test_groq_provider.py::test_live_calls_and_cache_are_off_by_default` | The shell environment carries `GROQ_LIVE_ENABLED=true` (local demo), and the process environment outranks env files in pydantic-settings. | Owner-approved isolation: the test removes `GROQ_*` and `LLM_*` from its environment, so it checks the code defaults. | `64e3e01` |
| K7b | `test_groq_provider.py::test_app_imports_without_the_groq_sdk_or_a_key` | The subprocess dropped the GROQ variables, but `get_settings()` also reads the repo-root `.env` (`GROQ_LIVE_ENABLED=true`). | Same isolation: the subprocess builds `Settings` from `infra/.env.example` only. | `64e3e01` |
| K8 | `test_dense_golden.py` (and `test_hybrid_dev.py`, which reads the same fixture) | Dense `search()` returned Qdrant's order for exact-score ties; Qdrant has no secondary sort key, and the golden fixture had recorded that arbitrary order. | Owner-approved deterministic tie-break (score descending, then chunk id), plus a one-pair fixture correction. | the A7.5 K8 commit |

## K7 and K7b: evidence

| Run | K7 | K7b |
|---|---|---|
| Environment as-is (`GROQ_LIVE_ENABLED=true` in the shell) | fail (`GROQ_LIVE_ENABLED` is True) | fail (`True True False`) |
| `GROQ_*`/`LLM_*` removed from the environment | pass | n/a |
| Only `GROQ_DATA_TERMS_VERIFIED_ON` removed | fail | n/a |
| Root `.env` temporarily hidden | n/a | pass |
| After the fix, environment unchanged | pass | pass |

The plan's K7 hypothesis was wrong. It named `GROQ_DATA_TERMS_VERIFIED_ON`, but the variable that decides the result is `GROQ_LIVE_ENABLED`.

## K8: evidence

- **Frequency:** the 20 golden queries were run 30 times, read-only. In this index state the result is deterministic (30/30 runs identical), but it differs from the snapshot in one query.
- **The tie:** two chunks in `hdfc_nri_account_opening` p.3 and `hdfc_resident_to_nro_conversion` p.3 have identical text, so their scores are identical to 7 decimals.
- **Query m1-03:** the tied pair scores 0.6943718 and straddles rank 5. Qdrant returned the NRI chunk at rank 5; the snapshot had the conversion chunk.
- **Query m1-14:** the tied pair scores 0.676856 at ranks 2 and 3. The snapshot had the NRI chunk first, which is the opposite order to m1-03.
- **Consequence:** the snapshot itself recorded an arbitrary, inconsistent order, and no deterministic rule reproduces both queries. Any rebuild or optimisation of the collection could flip either query, which is why the failure looked intermittent.

### Is the assistant affected?
- **Mode:** the assistant runs in `dense` mode through the same `search()`.
- **Measurement:** all 36 catalogue item titles were run in each of the 26 covered cells' opening scopes, plus the conversion scope: 972 searches.
- **Boundary ties: 0.** No tie fell at the top-5 boundary, so the set of chunks the assistant may cite never depended on Qdrant's internal order.
- **Ties inside the top 5: 10.** Only the order of tied supplementary references could vary.
- **The K8 pair cannot reach the assistant:** opening and conversion are separate scopes, so the two forms are never searched together.

### Fix
- **`app/retrieval/search.py`:**
  - fetches `k + 5` candidates;
  - if the tie still reaches the last candidate, doubles the limit, at most twice (bounded, CLAUDE.md §9);
  - sorts by (score descending, chunk id) and cuts to `k`.
- **Consistency:** this matches the existing tie-breaks of BM25, RRF fusion and reranking.
- **Regression test:** `tests/unit/test_dense_tie_break.py`. Any store order gives the same ranking, a tie that straddles rank k is ranked as a whole, and the refetch is bounded.
- **Fixture:** only `m1-14` ranks 2 and 3 (the tied pair) were swapped in `m1_dense_golden.json`, with the reason recorded in its description. Ids, scores, every other query and the metrics are unchanged, and every assertion stays exact.

### Verification after the fix

| Check | Result |
|---|---|
| Dense golden, hybrid dev, hybrid live and rerank live integration tests (17), run 3× | 17/17 passed each time |
| M1 dense metrics (recall / hit rate / MRR) | 0.825 / 0.9 / 0.775: identical to the snapshot |
| A2 assistant retrieval, Recall@5 (dense / BM25 / hybrid) | 0.733 / 0.613 / 0.793: identical to A2 and the A7 re-run |
| A7 coverage suite (55 profiles, 28 not-established, citation audit, golden item ids) | PASS; report identical apart from its header |
| `make test` | 2,097 passed, 0 failed |
