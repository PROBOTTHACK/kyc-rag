"""`form_analysis` node (M3 S4; decision D4).

Reads STORED form schemas only, through the `CorpusReader`. There is no multimodal
provider in the graph, so a live Gemini call is impossible here.

- Forms: KYC forms listing the customer's entity type (for INDIVIDUAL also the stated
  residency; for other entities residency is unknown and unused), plus forms whose
  chunks were retrieved. The NetBanking reference document is never a form.
- Schema: the latest complete `gemini_multimodal` row, else the latest `m1_chunks` row;
  the method, version, row id and schema hash are recorded.
- Fields: a bounded subset copied from the schema (sections holding a retrieved chunk,
  fields shown as required, declaration fields), each with its extraction status
  (OCR-uncertain / uncorroborated stays visible). `mandatory_under_policy` does not
  exist on these refs and stays null in the stored schemas: a form shows what it asks
  for, not what policy requires.
- A form with no stored schema is a recorded gap and degradation, never filled in.
"""

from typing import TYPE_CHECKING, Any

from app.graph.evidence import (
    candidate_forms,
    choose_schema_row,
    field_subset,
    review_flags,
)
from app.graph.state import (
    Degradation,
    EntityProfile,
    EvidenceItem,
    FormRef,
    KYCState,
    RetrievalMeta,
    dump,
    dump_list,
    read,
    read_list,
)

if TYPE_CHECKING:
    from app.graph.builder import GraphDeps


def form_analysis(state: KYCState, deps: "GraphDeps") -> dict[str, Any]:
    profile = read(state, "entity_profile", EntityProfile)
    if deps.corpus is None or profile is None:
        degradation = Degradation(component="forms", reason="not_configured")
        return {
            "forms": [],
            "degradations": [*(state.get("degradations") or []), dump(degradation)],
        }
    evidence = read_list(state, "evidence", EvidenceItem)
    retrieved_ids = {e.evidence_id for e in evidence}
    documents = candidate_forms(
        deps.corpus.form_documents(), profile, (e.document_key for e in evidence)
    )
    rows = deps.corpus.form_schema_rows()

    refs: list[FormRef] = []
    flags: list[str] = []
    gaps: list[str] = []
    degradations: list[Degradation] = []
    for doc in documents:
        key = doc.document_key
        row = choose_schema_row(rows, key)
        schema = deps.corpus.load_schema(key, row.method, row.version) if row else None
        if row is None or schema is None:
            reason = "no_stored_schema" if row is None else "schema_load_failed"
            flags.append(f"{key}:{reason}")
            gaps.append(f"{reason}:{key}")
            degradations.append(Degradation(component="forms", reason=f"{reason}:{key}"))
            continue
        fields, section_chunks, capped = field_subset(schema, retrieved_ids)
        form_flags = review_flags(schema, row, capped)
        refs.append(
            FormRef(
                form_key=key,
                method=row.method,
                version=row.version,
                row_id=row.row_id,
                schema_sha256=row.schema_sha256,
                fields=fields,
                flags=form_flags,
                section_chunk_ids=section_chunks,
            )
        )
        flags += [f"{key}:{f}" for f in form_flags]

    update: dict[str, Any] = {"forms": dump_list(refs), "form_flags": flags}
    if degradations:
        update["degradations"] = [*(state.get("degradations") or []), *dump_list(degradations)]
    meta = read(state, "retrieval_meta", RetrievalMeta)
    if gaps and meta is not None:
        update["retrieval_meta"] = dump(meta.model_copy(update={"gaps": [*meta.gaps, *gaps]}))
    return update
