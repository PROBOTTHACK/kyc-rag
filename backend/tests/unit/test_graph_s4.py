"""M3 S4: policy retrieval, form analysis and evidence association ("no evidence, no answer").

The retriever and the corpus reader are fakes built from real `ChunkMetadata` records
and real (m1) form schemas, so every evidence item and form field can be traced back.
The dev-stack and Postgres runs are in tests/integration/test_graph_s4_*.py.
"""

import asyncio
import inspect
import json
from collections.abc import Iterable
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import app.graph.corpus as corpus_module
import app.graph.evidence as evidence_module
import app.graph.nodes.evidence_association as association_node
import app.graph.nodes.form_analysis as form_node
import app.graph.nodes.policy_retrieval as retrieval_node
from app.db.repository import EvidenceContext
from app.graph.builder import GraphDeps, build_graph, run_config
from app.graph.corpus import FormDocument, FormSchemaRowInfo
from app.graph.evidence import (
    MAX_EVIDENCE_CHARS,
    MAX_FORM_FIELDS,
    MAX_LINKS_PER_FIELD,
    build_links,
    candidate_forms,
    choose_schema_row,
    classify_evidence,
    distinctive_tokens,
    field_subset,
    group_for,
)
from app.graph.inputs import resume_command, start_input
from app.graph.intake import ExtractedValue, IntakeExtraction
from app.graph.nodes.evidence_association import evidence_association
from app.graph.nodes.form_analysis import form_analysis
from app.graph.nodes.policy_retrieval import policy_retrieval
from app.graph.state import (
    EntityProfile,
    EvidenceItem,
    FormFieldRef,
    FormRef,
    ProfileValue,
    dump,
    read_list,
    validate_state,
)
from app.ingestion.chunking import ChunkRecord
from app.llm.fake import FakeLLMProvider, scripted_llm
from app.retrieval.retrieve import RetrievalError, RetrievalMode, RetrievalResult, RetrieverFailure
from app.retrieval.search import SearchFilters, SearchHit
from app.schemas.forms import FormSchema
from app.schemas.taxonomy import AccountType, ContentKind, EntityType
from tests.unit.test_forms_extract import form_doc, rec, schema
from tests.unit.test_indexing import doc

# --- synthetic corpus ---------------------------------------------------------------------------

RBI = doc("rbi_directions", entity_types=["INDIVIDUAL", "COMPANY", "PARTNERSHIP", "TRUST"])
FAQ = doc("rbi_faq", source_status="dated", status_reason="older basis")
NI_FORM = form_doc("hdfc_ni_form", entity_types=["LLP", "TRUST", "COMPANY", "HUF"])
NRI_FORM = form_doc("hdfc_nri_form", entity_types=["INDIVIDUAL"], residency_statuses=["NRI", "PIO"])
RES_FORM = form_doc("hdfc_res_form", entity_types=["INDIVIDUAL"], residency_statuses=["RESIDENT"])

PARA = ContentKind.PARAGRAPH
RBI_TRUST = rec(
    RBI,
    0,
    "35. For opening an account of a trust, certified copies of the trust "
    "deed and the PAN of the trust shall be obtained.",
    section="C",
    kind=PARA,
)
RBI_LEGAL = rec(
    RBI,
    1,
    "34. For a partnership firm: registration certificate, deed, PAN.",
    section="C",
    kind=PARA,
)
RBI_LONG = rec(RBI, 2, "x" * (MAX_EVIDENCE_CHARS + 50), section="C", kind=PARA)
FAQ_ITEM = rec(
    FAQ, 0, "Q6. Officially valid documents include the passport.", section="Q", kind=PARA
)
NI_FIELDS = rec(
    NI_FORM,
    0,
    "Fields: *Name of Trust | PAN of Trust | Registration Number",
    section="A) ENTITY DETAILS",
    kind=ContentKind.FORM_FIELDS,
)
NI_TEXT = rec(NI_FORM, 1, "Trust deed copy attached.", section="A) ENTITY DETAILS")
NI_DECL = rec(
    NI_FORM,
    2,
    "Fields: Signature of authorised signatory",
    section="D) DECLARATION",
    kind=ContentKind.FORM_FIELDS,
)
NI_OTHER = rec(
    NI_FORM,
    3,
    "Fields: Nominee name | Nominee address",
    section="C) NOMINATION",
    kind=ContentKind.FORM_FIELDS,
)
NRI_FIELDS = rec(
    NRI_FORM,
    0,
    "Fields: *Passport Number | Overseas address",
    section="B) NRI DETAILS",
    kind=ContentKind.FORM_FIELDS,
)

ALL = [RBI_TRUST, RBI_LEGAL, RBI_LONG, FAQ_ITEM, NI_FIELDS, NI_TEXT, NI_DECL, NI_OTHER, NRI_FIELDS]
NI_SCHEMA = schema(NI_FORM, [NI_FIELDS, NI_TEXT, NI_OTHER, NI_DECL])
NRI_SCHEMA = schema(NRI_FORM, [NRI_FIELDS])
SHA = "b" * 64


def cid(r: ChunkRecord) -> str:
    return r.metadata.chunk_id


def form_document(d: Any) -> FormDocument:
    return FormDocument(
        document_key=d.document_key,
        document_id=str(d.row_id),
        title=d.title,
        entity_types=tuple(str(e) for e in d.entity_types),
        residency_statuses=tuple(str(r) for r in d.residency_statuses),
    )


def row(form_key: str, method: str, version: int, *flags: str) -> FormSchemaRowInfo:
    return FormSchemaRowInfo(
        form_key=form_key,
        method=method,
        version=version,
        row_id=f"{form_key}-{method}-{version}",
        schema_sha256=SHA,
        quality_flags=tuple(flags),
    )


class FakeCorpus:
    """Read-only corpus over the synthetic records and schemas; records every lookup."""

    def __init__(
        self,
        records: Iterable[ChunkRecord] = ALL,
        rows: Iterable[FormSchemaRowInfo] | None = None,
        parents: dict[str, ChunkRecord] | None = None,
    ) -> None:
        self.records = {cid(r): r for r in records}
        self.parents = parents or {}
        self.docs = [form_document(d) for d in (NI_FORM, NRI_FORM, RES_FORM)]
        self.rows = (
            list(rows)
            if rows is not None
            else [row("hdfc_ni_form", "m1_chunks", 1), row("hdfc_nri_form", "m1_chunks", 1)]
        )
        self.schemas: dict[str, FormSchema] = {
            "hdfc_ni_form": NI_SCHEMA,
            "hdfc_nri_form": NRI_SCHEMA,
        }
        self.loaded: list[tuple[str, str, int]] = []

    def evidence(self, chunk_ids: Iterable[str]) -> dict[str, EvidenceContext]:
        out = {}
        for i in chunk_ids:
            if i in self.records:
                ctx = self.parents.get(i, self.records[i])
                out[i] = EvidenceContext(retrieved=self.records[i], context=ctx)
        return out

    def form_documents(self) -> list[FormDocument]:
        return list(self.docs)

    def form_schema_rows(self) -> list[FormSchemaRowInfo]:
        return list(self.rows)

    def load_schema(self, form_key: str, method: str, version: int) -> FormSchema | None:
        self.loaded.append((form_key, method, version))
        return self.schemas.get(form_key)


class FakeRetriever:
    """Scripted per entity term: a list of records (hits in order) or an exception."""

    def __init__(
        self,
        script: dict[str, Any],
        degraded: dict[str, tuple[str, ...]] | None = None,
        dropped: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.script, self.degraded, self.dropped = script, degraded or {}, dropped or {}
        self.calls: list[tuple[str, str, tuple[str, ...], bool]] = []

    def retrieve(
        self,
        query: str,
        *,
        mode: Any,
        top_k: int = 5,
        filters: SearchFilters | None = None,
        rerank: bool = False,
    ) -> RetrievalResult:
        terms = filters.entity_terms if filters else ()
        self.calls.append((query, str(mode), terms, rerank))
        value = self.script.get(terms[0] if terms else "", [])
        if isinstance(value, Exception):
            raise value
        hits = [
            SearchHit(
                rank=i + 1,
                score=1.0 / (i + 1),
                chunk_id=cid(r),
                payload=r.metadata.to_qdrant_payload(),
            )
            for i, r in enumerate(value[:top_k])
        ]
        return RetrievalResult(
            mode=RetrievalMode(mode),
            hits=hits,
            provenance={},
            degraded=self.degraded.get(terms[0], ()) if terms else (),
            dropped=self.dropped.get(terms[0], ()) if terms else (),
            timings_ms={"dense_ms": 1.0},
        )


def profile(
    entity: str, residency: str | None = None, account: str = "CURRENT_ACCOUNT"
) -> EntityProfile:
    return EntityProfile(
        entity_type=ProfileValue(value=entity, source="user_answered"),
        residency=(
            ProfileValue(value=residency, source="user_answered") if residency else ProfileValue()
        ),
        account_type=ProfileValue(value=account, source="user_answered"),
    )


def state_for(p: EntityProfile, **extra: Any) -> dict[str, Any]:
    return {
        "query": "What documents are needed?",
        "security_status": "safe",
        "intake_complete": True,
        "entity_profile": dump(p),
    } | extra


def deps(retriever: Any = None, corpus: Any = None, **kw: Any) -> GraphDeps:
    return GraphDeps(retriever=retriever, corpus=corpus, **kw)


def items(update: dict[str, Any]) -> list[EvidenceItem]:
    return [EvidenceItem.model_validate(i) for i in update["evidence"]]


# --- the four evidence statuses ----------------------------------------------------------------


def test_found_entity_primary_evidence_with_full_provenance_and_no_group_pass() -> None:
    retriever = FakeRetriever({"TRUST": [RBI_TRUST, NI_FIELDS]})
    out = policy_retrieval(state_for(profile("TRUST")), deps(retriever, FakeCorpus()))
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "found" and meta["gaps"] == [] and meta["failed"] is False
    assert [c[2] for c in retriever.calls] == [("TRUST",)]  # no group pass needed
    first = items(out)[0]
    assert (first.evidence_id, first.document_key, first.page, first.match_scope) == (
        cid(RBI_TRUST),
        "rbi_directions",
        1,
        "entity",
    )
    assert first.can_support_mandatory and first.evidence_eligibility == "primary"
    assert out["filters"]["entity_terms"] == ["TRUST"]
    assert out["form_analysis_required"] is True  # a stored TRUST form exists


def test_partial_when_only_the_entity_group_has_primary_evidence() -> None:
    retriever = FakeRetriever({"LLP": [NI_FIELDS], "LEGAL_ENTITY": [NI_FIELDS, RBI_LEGAL]})
    out = policy_retrieval(state_for(profile("LLP")), deps(retriever, FakeCorpus()))
    assert [c[2] for c in retriever.calls] == [("LLP",), ("LEGAL_ENTITY",)]
    got = items(out)
    assert [(i.evidence_id, i.match_scope, i.rank) for i in got] == [
        (cid(NI_FIELDS), "entity", 1),
        (cid(RBI_LEGAL), "entity_group", 2),
    ]  # (the duplicate form chunk from the group pass is not repeated)
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "partial"
    assert meta["gaps"] == [
        "no_entity_specific_primary_evidence",
        "primary_evidence_only_via_entity_group",
    ]


def test_partial_supporting_only_for_an_individual_never_runs_a_group_pass() -> None:
    retriever = FakeRetriever({"INDIVIDUAL": [NRI_FIELDS, FAQ_ITEM]})
    out = policy_retrieval(state_for(profile("INDIVIDUAL", "NRI")), deps(retriever, FakeCorpus()))
    assert len(retriever.calls) == 1
    assert out["retrieval_meta"]["evidence_status"] == "partial"
    assert out["retrieval_meta"]["gaps"] == ["no_entity_specific_primary_evidence"]


@pytest.mark.parametrize(
    ("entity", "group"),
    [
        ("LLP", "LEGAL_ENTITY"),
        ("TRUST", "LEGAL_ENTITY"),
        ("HUF", "NON_INDIVIDUAL"),
        ("SOLE_PROPRIETORSHIP", "NON_INDIVIDUAL"),
        ("INDIVIDUAL", None),
    ],
)
def test_group_choice_uses_the_existing_taxonomy(entity: str, group: str | None) -> None:
    found = group_for(EntityType(entity))
    assert (found.value if found else None) == group


def test_none_is_a_successful_empty_search_not_a_failure() -> None:
    out = policy_retrieval(state_for(profile("LLP")), deps(FakeRetriever({}), FakeCorpus()))
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "none" and meta["failed"] is False
    assert meta["gaps"] == ["no_eligible_evidence"] and meta["failure_kinds"] == []
    assert out["evidence"] == [] and "degradations" not in out


@pytest.mark.parametrize(
    ("retriever", "corpus", "kinds"),
    [
        (
            FakeRetriever(
                {"TRUST": RetrievalError([RetrieverFailure("dense", "ConnectError: refused")])}
            ),
            FakeCorpus(),
            ["dense:ConnectError"],
        ),
        (None, FakeCorpus(), ["not_configured"]),
        (FakeRetriever({"TRUST": [RBI_TRUST]}), None, ["not_configured"]),
    ],
)
def test_retrieval_failed_is_distinct_from_no_evidence(
    retriever: Any, corpus: Any, kinds: list[str]
) -> None:
    out = policy_retrieval(state_for(profile("TRUST")), deps(retriever, corpus))
    meta = out["retrieval_meta"]
    assert meta["evidence_status"] == "retrieval_failed" and meta["failed"] is True
    assert meta["failure_kinds"] == kinds and meta["gaps"] == ["retrieval_failed"]
    assert out["degradations"][-1]["component"] == "retrieval"
    assert "refused" not in json.dumps(out)  # class names only, never messages


def test_none_and_failure_both_end_insufficient_evidence_but_stay_distinguishable() -> None:
    results = {}
    for name, retriever in (
        ("none", FakeRetriever({})),
        ("failed", FakeRetriever({"LLP": RetrievalError([])})),
    ):
        g = build_graph(
            deps(retriever, FakeCorpus(), llm=scripted_llm(LLP_CURRENT)),
            checkpointer=InMemorySaver(),
        )
        out = asyncio.run(
            g.ainvoke(
                start_input(LLP_CURRENT_QUERY, session_id="s", run_id="r"),
                run_config(GraphDeps(), thread_id=name),
            )
        )
        results[name] = out
    assert results["none"]["outcome"] == results["failed"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert results["none"]["retrieval_meta"]["evidence_status"] == "none"
    assert results["failed"]["retrieval_meta"]["evidence_status"] == "retrieval_failed"
    assert "degradations" not in results["none"]
    assert results["failed"]["degradations"][0]["component"] == "retrieval"


# --- retrieval details ----------------------------------------------------------------------------


def test_a_failed_group_pass_keeps_the_entity_evidence_and_is_recorded() -> None:
    retriever = FakeRetriever({"LLP": [NI_FIELDS], "LEGAL_ENTITY": RetrievalError([])})
    out = policy_retrieval(state_for(profile("LLP")), deps(retriever, FakeCorpus()))
    assert [i.evidence_id for i in items(out)] == [cid(NI_FIELDS)]
    assert out["retrieval_meta"]["evidence_status"] == "partial"
    assert out["degradations"] == [
        {"component": "retrieval", "reason": "group_pass:RetrievalError"}
    ]


def test_degraded_hybrid_and_rerank_are_recorded_as_degradations() -> None:
    retriever = FakeRetriever({"TRUST": [RBI_TRUST]}, degraded={"TRUST": ("bm25", "rerank")})
    out = policy_retrieval(state_for(profile("TRUST")), deps(retriever, FakeCorpus()))
    assert out["retrieval_meta"]["degraded"] == ["bm25", "rerank"]
    assert out["degradations"] == [
        {"component": "retrieval", "reason": "bm25_failed"},
        {"component": "rerank", "reason": "rerank_failed"},
    ]


def test_hits_without_a_stored_row_are_dropped_and_counted_never_cited() -> None:
    ghost = rec(RBI, 9, "not stored", section="C", kind=PARA)
    retriever = FakeRetriever({"TRUST": [ghost, RBI_TRUST]}, dropped={"TRUST": ("x",)})
    out = policy_retrieval(state_for(profile("TRUST")), deps(retriever, FakeCorpus()))
    assert [i.evidence_id for i in items(out)] == [cid(RBI_TRUST)]
    assert items(out)[0].rank == 1 and out["retrieval_meta"]["dropped_count"] == 2


def test_parent_expansion_and_truncation_are_flagged() -> None:
    corpus = FakeCorpus(parents={cid(RBI_TRUST): RBI_LONG})
    out = policy_retrieval(
        state_for(profile("TRUST")), deps(FakeRetriever({"TRUST": [RBI_TRUST]}), corpus)
    )
    item = items(out)[0]
    assert item.evidence_id == cid(RBI_TRUST) and item.expanded_parent_id == cid(RBI_LONG)
    assert item.text_truncated and len(item.text) == MAX_EVIDENCE_CHARS


def test_settings_mode_and_rerank_are_passed_to_the_retriever() -> None:
    retriever = FakeRetriever({"TRUST": [RBI_TRUST]})
    out = policy_retrieval(
        state_for(profile("TRUST")),
        deps(retriever, FakeCorpus(), retrieval_mode="bm25", rerank=True),
    )
    assert retriever.calls[0][1:] == ("bm25", ("TRUST",), True)
    assert out["retrieval_meta"]["mode"] == "bm25"


def test_every_evidence_item_traces_to_a_stored_chunk() -> None:
    corpus = FakeCorpus()
    retriever = FakeRetriever({"LLP": [NI_FIELDS, NI_TEXT], "LEGAL_ENTITY": [RBI_LEGAL, FAQ_ITEM]})
    out = policy_retrieval(state_for(profile("LLP")), deps(retriever, corpus))
    for item in items(out):
        stored = corpus.records[item.evidence_id].metadata
        assert (item.document_key, item.page, item.bank) == (
            stored.document_key,
            stored.page_number,
            stored.bank,
        )
        assert item.text == stored.content


# --- form analysis --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p", "retrieved", "expected"),
    [
        (profile("LLP"), [], ["hdfc_ni_form"]),
        (profile("LLP", "RESIDENT"), [], ["hdfc_ni_form"]),  # residency unused for non-individuals
        (profile("INDIVIDUAL", "RESIDENT"), [], ["hdfc_res_form"]),
        (profile("INDIVIDUAL", "NRI"), [], ["hdfc_nri_form"]),
        (profile("INDIVIDUAL", "PIO"), [], ["hdfc_nri_form"]),
        (profile("SOCIETY"), [], []),
        (profile("SOCIETY"), ["hdfc_ni_form"], ["hdfc_ni_form"]),  # a retrieved form chunk
    ],
)
def test_candidate_forms_follow_the_approved_residency_rule(
    p: EntityProfile, retrieved: list[str], expected: list[str]
) -> None:
    docs = FakeCorpus().form_documents()
    assert [d.document_key for d in candidate_forms(docs, p, retrieved)] == expected


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            [
                row("f", "m1_chunks", 1),
                row("f", "gemini_multimodal", 1, "multimodal_model_output"),
                row("f", "gemini_multimodal", 2),
            ],
            ("gemini_multimodal", 2),
        ),
        (
            [
                row("f", "m1_chunks", 1),
                row("f", "gemini_multimodal", 1),
                row("f", "gemini_multimodal", 2, "partial_extraction"),
            ],
            ("gemini_multimodal", 1),
        ),
        (
            [
                row("f", "m1_chunks", 1),
                row("f", "gemini_multimodal", 1, "gemini_extraction_refused"),
            ],
            ("m1_chunks", 1),
        ),
        ([row("other", "m1_chunks", 1)], None),
    ],
)
def test_schema_choice_prefers_the_latest_complete_gemini_row(
    rows: list[FormSchemaRowInfo], expected: tuple[str, int] | None
) -> None:
    chosen = choose_schema_row(rows, "f")
    assert ((chosen.method, chosen.version) if chosen else None) == expected


def test_form_analysis_records_the_row_and_copies_fields_with_their_status() -> None:
    corpus = FakeCorpus()
    retrieved = dump(EvidenceItem.model_validate(evidence_dict(NI_TEXT)))
    out = form_analysis(state_for(profile("LLP"), evidence=[retrieved]), deps(corpus=corpus))
    [ref] = [FormRef.model_validate(f) for f in out["forms"]]
    assert (ref.form_key, ref.method, ref.version, ref.row_id) == (
        "hdfc_ni_form",
        "m1_chunks",
        1,
        "hdfc_ni_form-m1_chunks-1",
    )
    labels = [f.label for f in ref.fields]
    # section of the retrieved chunk + declaration; the unrelated nomination section is out
    assert labels == [
        "Name of Trust",
        "PAN of Trust",
        "Registration Number",
        "Signature of authorised signatory",
    ]
    assert ref.fields[0].required_as_shown is True and ref.fields[0].section == "A) ENTITY DETAILS"
    stored = {
        (f.printed_label, f.chunk_id, f.verification_status)
        for s in NI_SCHEMA.sections
        for f in s.fields
    }
    assert all((f.printed_label, f.chunk_id, f.verification_status) in stored for f in ref.fields)
    assert corpus.loaded == [("hdfc_ni_form", "m1_chunks", 1)]
    assert "mandatory_under_policy" not in json.dumps(out)


def test_a_form_without_a_stored_schema_is_a_gap_never_filled_in() -> None:
    corpus = FakeCorpus(rows=[])
    meta = {"mode": "dense", "failed": False, "evidence_status": "found", "gaps": []}
    out = form_analysis(state_for(profile("LLP"), retrieval_meta=meta), deps(corpus=corpus))
    assert out["forms"] == [] and out["form_flags"] == ["hdfc_ni_form:no_stored_schema"]
    assert out["retrieval_meta"]["gaps"] == ["no_stored_schema:hdfc_ni_form"]
    assert out["degradations"] == [
        {"component": "forms", "reason": "no_stored_schema:hdfc_ni_form"}
    ]
    assert corpus.loaded == []


def test_field_subset_is_capped_and_flagged() -> None:
    many = rec(
        NI_FORM,
        0,
        "Fields: " + " | ".join(f"*Field {i}" for i in range(80)),
        section="A) BIG",
        kind=ContentKind.FORM_FIELDS,
    )
    fields, sections, capped = field_subset(schema(NI_FORM, [many]), set())
    assert len(fields) == MAX_FORM_FIELDS and capped and list(sections) == ["A) BIG"]


def test_extraction_status_is_preserved_never_upgraded() -> None:
    section = NI_SCHEMA.sections[0]
    field = section.fields[0].model_copy(update={"verification_status": "uncorroborated"})
    changed = NI_SCHEMA.model_copy(
        update={
            "sections": [section.model_copy(update={"fields": [field]}), *NI_SCHEMA.sections[1:]]
        }
    )
    fields, _, _ = field_subset(changed, {cid(NI_FIELDS)})
    assert fields[0].verification_status == "uncorroborated"


def test_form_analysis_never_touches_a_multimodal_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.llm.gemini as gemini

    def forbidden(*a: Any, **k: Any) -> Any:
        raise AssertionError("Gemini must never be called by the workflow")

    monkeypatch.setattr(gemini, "multimodal_from_settings", forbidden)
    monkeypatch.setattr(gemini.GeminiProvider, "analyze_pages", forbidden)
    monkeypatch.setattr(gemini.GeminiProvider, "from_settings", forbidden)
    out = form_analysis(state_for(profile("INDIVIDUAL", "NRI")), deps(corpus=FakeCorpus()))
    assert [f["form_key"] for f in out["forms"]] == ["hdfc_nri_form"]


def test_form_refs_have_no_obligation_field() -> None:
    assert "mandatory_under_policy" not in FormFieldRef.model_fields
    assert "mandatory_under_policy" not in FormRef.model_fields


# --- evidence association -------------------------------------------------------------------------


def evidence_dict(r: ChunkRecord, rank: int = 1) -> dict[str, Any]:
    m = r.metadata
    return {
        "evidence_id": m.chunk_id,
        "document_id": m.document_id,
        "document_key": m.document_key,
        "bank": m.bank,
        "page": m.page_number,
        "source_status": m.source_status,
        "evidence_eligibility": m.evidence_eligibility,
        "text_reliability": m.text_reliability,
        "can_support_mandatory": m.can_support_mandatory,
        "rank": rank,
        "score_kind": "dense_cosine",
        "text": m.content,
        "section": m.section,
    }


def ni_ref(retrieved: set[str]) -> FormRef:
    fields, sections, _ = field_subset(NI_SCHEMA, retrieved)
    return FormRef(
        form_key="hdfc_ni_form",
        method="m1_chunks",
        version=1,
        row_id="r",
        schema_sha256=SHA,
        fields=fields,
        section_chunk_ids=sections,
    )


def test_the_three_link_kinds_are_relationships_only() -> None:
    ev = [
        EvidenceItem.model_validate(evidence_dict(r, i + 1))
        for i, r in enumerate([NI_FIELDS, NI_TEXT, RBI_TRUST])
    ]
    ref = ni_ref({cid(NI_FIELDS), cid(NI_TEXT)})
    links = build_links(ev, [(ref.form_key, ref.fields, ref.section_chunk_ids)], {"hdfc_ni_form"})
    by_field = {}
    for link in links:
        by_field.setdefault(link.field_label, []).append((link.link_kind.value, link.evidence_id))
        assert link.relationship_only is True
    assert by_field["Name of Trust"][:2] == [
        ("same_chunk", cid(NI_FIELDS)),
        ("same_section", cid(NI_TEXT)),
    ]
    assert ("topic_match", cid(RBI_TRUST)) in by_field["PAN of Trust"]
    assert all(len(v) <= MAX_LINKS_PER_FIELD for v in by_field.values())
    # deterministic
    assert links == build_links(
        ev, [(ref.form_key, ref.fields, ref.section_chunk_ids)], {"hdfc_ni_form"}
    )


def test_generic_labels_never_create_topic_matches() -> None:
    assert distinctive_tokens("Name") == set()
    assert distinctive_tokens("Date of Birth") == {"birth"}
    assert distinctive_tokens("PAN of Trust") == {"pan", "trust"}


def test_the_association_node_writes_typed_links() -> None:
    ev = [evidence_dict(NI_FIELDS), evidence_dict(RBI_TRUST, 2)]
    ref = ni_ref({cid(NI_FIELDS)})
    state = state_for(profile("LLP"), evidence=ev, forms=[dump(ref)])
    out = evidence_association(state, deps(corpus=FakeCorpus()))
    assert out["associations"] and all(a["relationship_only"] is True for a in out["associations"])
    assert {a["link_kind"] for a in out["associations"]} <= {
        "same_chunk",
        "same_section",
        "topic_match",
    }


# --- the whole S4 path through the graph ----------------------------------------------------------

LLP_QUERY = "What KYC documents does an LLP need to open an account?"
LLP_CURRENT_QUERY = "What KYC documents does an LLP need to open a current account?"
LLP_CURRENT = IntakeExtraction(
    entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(value="CURRENT_ACCOUNT", evidence_span="a current account"),
)
LLP_COMPLETE = IntakeExtraction(
    entity_type=ExtractedValue(value="LLP", evidence_span="an LLP"),
    residency=ExtractedValue(),
    account_type=ExtractedValue(),
)


def test_llp_end_to_end_with_clarification_partial_evidence_forms_and_links() -> None:
    from app.graph.synthesis import ChecklistDraft, DraftRequirement

    draft = ChecklistDraft(
        requirements=[
            DraftRequirement(
                name="Partnership deed",
                explanation="From the cited group-scope paragraph.",
                applicability="MANDATORY",
                evidence_ids=[cid(RBI_LEGAL)],
            )
        ]
    )
    llm = scripted_llm(LLP_COMPLETE, draft)
    retriever = FakeRetriever({"LLP": [NI_FIELDS, NI_TEXT], "LEGAL_ENTITY": [RBI_LEGAL, RBI_TRUST]})
    d = deps(retriever, FakeCorpus(), llm=llm)
    g = build_graph(d, checkpointer=InMemorySaver())
    config = run_config(d, thread_id="llp")
    paused = asyncio.run(g.ainvoke(start_input(LLP_QUERY, session_id="s", run_id="r"), config))
    assert paused["__interrupt__"][0].value["questions"][0]["field"] == "account_type"
    final = asyncio.run(
        g.ainvoke(resume_command({"answers": {"account_type": "CURRENT_ACCOUNT"}}), config)
    )
    meta = final["retrieval_meta"]
    assert meta["evidence_status"] == "partial"
    assert "no_entity_specific_primary_evidence" in meta["gaps"]
    scopes = {e["match_scope"] for e in final["evidence"]}
    assert scopes == {"entity", "entity_group"}
    assert [f["form_key"] for f in final["forms"]] == ["hdfc_ni_form"]
    assert final["associations"]
    # S5: entity-group evidence can never establish MANDATORY on its own
    assert [r["applicability"] for r in final["checklist"]] == ["CONDITIONAL"]
    # M4 S5: claim verification ran with NLI disabled: not verified, honestly DEGRADED
    assert final["outcome"] == "DEGRADED"
    assert [d["component"] for d in final["degradations"]] == ["verification"]
    assert {c["requirement_verdict"] for c in final["claim_results"]} == {"not_verified"}
    assert len(llm.requests) == 2  # intake + synthesis; the S4 nodes make none
    validate_state(final)
    assert "mandatory_under_policy" not in json.dumps(final)


def test_s4_nodes_never_call_the_llm() -> None:
    llm = FakeLLMProvider([])  # raises if called
    d = deps(FakeRetriever({"TRUST": [RBI_TRUST]}), FakeCorpus(), llm=llm)
    state = state_for(profile("TRUST"))
    state |= policy_retrieval(state, d)
    state |= form_analysis(state, d)
    state |= evidence_association(state, d)
    assert llm.requests == [] and read_list(state, "evidence", EvidenceItem)


# --- approved decisions are reused, never redefined -----------------------------------------------


def test_s4_defines_no_vocabulary_of_its_own() -> None:
    assert [a.value for a in AccountType] == [
        "SAVINGS_ACCOUNT",
        "CURRENT_ACCOUNT",
        "NRE_ACCOUNT",
        "NRO_ACCOUNT",
        "TERM_DEPOSIT",
    ]
    for module in (evidence_module, corpus_module, retrieval_node, form_node, association_node):
        source = inspect.getsource(module)
        assert "StrEnum" not in source, module.__name__
        for value in [
            *(a.value for a in AccountType),
            "RESIDENT",
            "LEGAL_ENTITY",
            "NON_INDIVIDUAL",
        ]:
            assert f'"{value}"' not in source, (module.__name__, value)


def test_classify_evidence_has_exactly_three_search_statuses() -> None:
    entity_primary = EvidenceItem.model_validate(evidence_dict(RBI_TRUST))
    group_primary = entity_primary.model_copy(update={"match_scope": "entity_group"})
    supporting = EvidenceItem.model_validate(evidence_dict(NI_FIELDS))
    assert classify_evidence([]) == ("none", ["no_eligible_evidence"])
    assert classify_evidence([supporting, entity_primary])[0] == "found"
    assert classify_evidence([supporting, group_primary]) == (
        "partial",
        ["no_entity_specific_primary_evidence", "primary_evidence_only_via_entity_group"],
    )
    assert classify_evidence([supporting]) == ("partial", ["no_entity_specific_primary_evidence"])
