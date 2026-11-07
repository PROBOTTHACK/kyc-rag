# 13 — Interview Prep

Answer each using YOUR implementation decisions and measured results.
1. Why LangGraph over a plain LangChain chain? (stateful cycles, interrupts, checkpoints, conditional routing)
2. Why two LLM providers? (Groq speed/structured text; Gemini multimodal; provider interface + fallback)
3. Why Qdrant for retrieval but Postgres for state/reports/chunk text? Why not Qdrant only?
4. Why hybrid search + RRF? Show your Recall@5 comparison.
5. Why is form analysis separate from policy retrieval? (different modality, cached schema, field presence ≠ mandatory)
6. Why isn't NLI enough for legal correctness? (entailment only; authority/currency/applicability unknown)
7. How do you handle conflicting documents? (source hierarchy, conflict flag, human review)
8. How do you stop prompt injection via retrieved PDFs? (untrusted evidence, delimiters, ingestion scan, least privilege)
9. How do you prevent infinite agent loops? (max 2 regenerations, step limits, timeouts)
10. How do you measure hallucination reduction? (ablation A vs D vs E, unsupported-claim rate)
11. What happens on Groq/Gemini rate limits? (Redis buckets, cache, backoff, fallback, degrade)
12. What happens when evidence is missing? (UNCLEAR/Unverified, never fabricated)
13. Why Redis? (rate limits, cache, pub/sub SSE, queue, locks)
14. How do you keep it free? (local Docker + free tiers, caching, small local models)
