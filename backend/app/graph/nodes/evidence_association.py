"""`evidence_association` node (M3 S4; M3_PLAN §7).

Deterministic links between the analysed form fields and the retrieved evidence:
`same_chunk`, `same_section` and `topic_match` (`app.graph.evidence.build_links`).

These are EVIDENCE RELATIONSHIPS ONLY ("this passage is related to this field"). They are
not citations, never proof of a legal obligation, never set `mandatory_under_policy`,
and cannot raise anything to MANDATORY: only the S5 output validator decides that, from
cited primary evidence. No LLM is involved.
"""

from typing import TYPE_CHECKING, Any

from app.graph.evidence import build_links
from app.graph.state import EvidenceItem, FormRef, KYCState, dump_list, read_list

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def evidence_association(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    evidence = read_list(state, "evidence", EvidenceItem)
    forms = read_list(state, "forms", FormRef)
    # Form documents are known to the corpus; without it, at least the analysed forms.
    if deps.corpus is not None:
        form_keys = {d.document_key for d in deps.corpus.form_documents()}
    else:
        form_keys = {f.form_key for f in forms}
    links = build_links(
        evidence,
        [(f.form_key, f.fields, f.section_chunk_ids) for f in forms],
        form_keys,
    )
    return {"associations": dump_list(links)}
