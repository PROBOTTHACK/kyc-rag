# 10 — Evaluation & Testing

## Functional tests
Individual question -> correct entity; LLP -> LLP retrieval; private company -> company retrieval; missing entity -> clarification; form question -> multimodal path; policy-only -> form analysis skipped; unsupported question -> explicit uncertainty.

## Adversarial tests
Version conflict: when the 2016-based RBI FAQ (or the dated HDFC FAQ) and the 2025 RBI Directions differ, the system prefers the current one and flags the difference.
Prompt injection -> blocked/safe; malicious PDF instruction -> treated as data; system-prompt request -> refused; fake requirement -> not asserted; contradictory docs -> flagged; missing evidence -> unverified.

## Benchmark (start 50 cases, grow to 100+), stored in `data/eval/*.jsonl`
| Category | Cases |
|---|---|
| Normal entity-specific questions | 15 |
| Missing information | 10 |
| Entity classification | 5 |
| Prompt injection | 5 |
| Unsupported / contradictory claims | 10 |
| Multimodal form interpretation | 5 |
Label by hand from the real source PDFs (expected entity, expected evidence page(s), expected requirement set/uncertainty).

## M1 retrieval benchmark (implemented)
`data/eval/m1_retrieval.jsonl` has 20 queries with 28 target passages, written as natural customer/officer questions on topics the Stage D development probes did not use. Each target is `(document_key, physical page[, paragraph][, text_contains])`. Targets were labelled from the source text: every page and paragraph was checked against the stored chunk text before the first evaluation run, and the test suite checks that every target matches an eligible chunk. The labels were not changed after results were seen. One annotator: treat the numbers as a baseline, not a precise estimate.
- Matching: same document; the hit's page range covers the target page; paragraph label and `text_contains` must also match when given.
- Metrics: mean Recall@5 (matched targets / targets, per query), Hit@5 (≥ 1 target in the top 5), MRR@5. Dense BGE-small search with the production eligibility filter (primary + supporting).
- Run: `make eval` (`python -m app.ingestion.cli evaluate`).
- **Result (2026-11-08, `policy_chunks`, BGE-small-en-v1.5 @ `5c38ec7c…`):** Recall@5 **0.825**, Hit@5 **0.90**, MRR@5 **0.775**. Misses:
  - m1-01 (sole proprietorship): paras 33, 34 and 30 outrank para 31;
  - m1-05 (FPI): the p.50 half of the table is not in the top 5;
  - m1-06 (no OVD): 169MD para 28 is missing (FAQ Q6 found at rank 3);
  - m1-19 (company beneficial owner): the neighbouring definitions child ranks first;
  - m1-20 (NRO conversion): another chunk of the right form and page is found, not the labelled request sentence.

  These are the M2 baseline that hybrid BM25 + RRF must beat.

## Metrics
Retrieval **Recall@5**, **citation precision**, **unsupported-claim rate**, claim-verification precision/recall, evidence coverage, false-rejection rate, entity-classification accuracy, form-extraction accuracy, **security attack success rate**, end-to-end latency, token usage.

## Ablations
| Exp | Architecture |
|---|---|
| A | Basic RAG, no claim verification |
| B | RAG + metadata filtering |
| C | RAG + hybrid retrieval |
| D | RAG + claim-level NLI |
| E | Full pipeline (security + human review) |
Report accuracy, unsupported rate, citation quality, latency, tokens. **Don't assume E wins** — let the numbers decide and report honestly.

## Tooling
`pytest` for unit/integration; `make eval` runs the benchmark per config and writes `eval_runs/eval_results` + a markdown report. Keep LLM judge usage minimal (quota); prefer deterministic checks.
