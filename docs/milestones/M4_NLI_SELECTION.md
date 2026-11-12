# M4 S4 NLI selection (dev set only)

- **Protocol:** `m4-s4-v1`, run once at commit `0c4cc4996cf32b3c51106c14501fbe02ccabbfa2`.
- **Dev file SHA-256:** `b199e4da0849950ea78a6d7985f8cbb7ff52137a7bf77c8ee97a2cc5e353b0f7`.
- **Held-out set:** not read for scoring (S8 only).
- **Qualifying configurations:** 0 of 140.

**No configuration qualified.** NLI stays off by default (decision D5).

## Models

- N1 `cross-encoder/nli-deberta-v3-xsmall@a1508764`: loaded
- N2 `cross-encoder/nli-deberta-v3-small@fa280487`: loaded
- N3 `cross-encoder/nli-deberta-v3-base@6c749ce3`: loaded
- N4 `MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli@6f5cf0a2`: loaded

## Limitations

Thresholds and checkpoint were chosen on 55 dev cases drafted by one annotator who also built the verifier; the choice overfits this set. The one-time held-out run (S8) is the only unbiased estimate and is reported as is. Scores are uncalibrated model signals, never a confidence. Bootstrap intervals are reported, never decisive.
