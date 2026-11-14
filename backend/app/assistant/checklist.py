"""Evidence-backed checklist generation (A4; A1 §3, §4, §7, §8). No LangGraph, no LLM.

Data flow for one confirmed profile (`ReadyProfile`, from the A3 dialogue):
1. **validate**: the profile is complete and agrees with the coverage map (recomputed, never
   trusted); a NOT_ESTABLISHED cell returns the prescribed fallback.
2. **select**: catalogue items for the profile's scope, category, account type and facets.
3. **gather**: each item's cited chunks, plus (optionally) chunks found by the scoped,
   HDFC-only retriever that contain one of the item's exact quotes. Retrieval can only add
   references to an item; it can never create an item.
4. **resolve and validate**: every chunk is re-read from Postgres; a citation survives only if
   the chunk exists, is HDFC, is in the cell's permitted sources, has the claimed document and
   page, and contains the quote verbatim. An item left without a valid catalogue citation is
   dropped (and, if the customer asked about it, reported as `unknown`).
5. **classify** deterministically from the source role, and **assemble** the three groups.

Nothing here can produce `policy_supported` with today's corpus, cite a re-KYC / legacy /
conversion source for an opening request, or show OCR-uncertain text as a quotation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.assistant.catalogue import Catalogue, CatalogueItem, NotConfirmedEntry, catalogue
from app.assistant.chunks import ChunkSource, StoredChunk
from app.assistant.coverage import Coverage, coverage
from app.assistant.dialogue import ReadyProfile
from app.assistant.profile import Slot, slot_for_facet, valid_value
from app.assistant.retrieval import AssistantRetriever, AssistantScopeError
from app.assistant.taxonomy import (
    ASSISTANT_BANK,
    AccountType,
    CoverageState,
    CustomerCategory,
    Intent,
    SourceRole,
)
from app.assistant.understand import Topic

log = logging.getLogger(__name__)

QUOTABLE = frozenset({"native", "ocr_reliable"})  # A1 §8.2 rule 3a
RETRIEVAL_TOP_K = 5
# Retrieval may add a reference only for a distinctive quote: short field labels ("Date of
# Birth", "Nationality") also occur in unrelated sections and would mislead.
MIN_SUPPLEMENTARY_QUOTE = 24


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Classification(StrEnum):
    POLICY_SUPPORTED = "policy_supported"
    FORM_INDICATED = "form_indicated"
    UNCONFIRMED = "unconfirmed"
    LEGACY_REFERENCE = "legacy_reference"
    UNKNOWN = "unknown"


def classify(role: SourceRole | None) -> Classification:
    """A1 §3: the class follows the tier of the item's basis, nothing else.
    Tier 1 (policy) is the only route to `policy_supported`; no policy exists in the corpus."""
    if role is SourceRole.POLICY:
        return Classification.POLICY_SUPPORTED
    if role in (SourceRole.OPENING_FORM, SourceRole.CONVERSION_FORM):
        return Classification.FORM_INDICATED
    if role is SourceRole.CONTEXT:
        return Classification.UNCONFIRMED
    if role is SourceRole.LEGACY_REFERENCE:
        return Classification.LEGACY_REFERENCE
    raise ValueError(f"no classification for a {role} source")


class InvalidProfile(ValueError):
    """The profile is incomplete or contradicts the coverage map (a caller error)."""


# --- the answer ----------------------------------------------------------------------------------


class AnswerKind(StrEnum):
    CHECKLIST = "checklist"
    FALLBACK = "fallback"
    TECHNICAL_ERROR = "technical_error"


class CaveatCode(StrEnum):
    DATED_SOURCE = "dated_source"
    SCANNED_TEXT = "scanned_text"
    CONDITIONAL = "conditional"


class Caveat(_Frozen):
    code: CaveatCode
    text: str


class Citation(_Frozen):
    chunk_id: str = Field(exclude=True)  # internal: never serialised to the customer (A1 §8.2.6)
    document_key: str
    document_title: str
    page: int
    page_end: int | None
    section: str | None
    text_reliability: str
    extraction_method: str
    date_basis: str
    quotable: bool
    basis: bool  # True: cited by the catalogue; False: the same wording found elsewhere
    passage: str  # the stored chunk text, shown on expand


class ChecklistItem(_Frozen):
    id: str
    title: str
    kind: str
    classification: Classification
    classification_label: str
    condition: str | None = None
    applies_if: tuple[str, ...] = ()
    source_wording: str | None = None  # exact quotation: reliable text only
    approximate_reading: str | None = None  # OCR-uncertain text, never shown as a quotation
    uncertainty_warning: str | None = None
    establishes: str | None = None
    does_not_establish: str
    note: str | None = None
    citations: tuple[Citation, ...] = ()
    related_citations: tuple[Citation, ...] = ()  # context for an assumption; never a basis
    caveats: tuple[Caveat, ...] = ()
    asked: bool = False
    fallback: str | None = None


class ChecklistGroups(_Frozen):
    documents: tuple[ChecklistItem, ...]  # A: documents and declarations the form asks for
    information: tuple[ChecklistItem, ...]  # B: information the form asks for
    not_confirmed: tuple[ChecklistItem, ...]  # C: could not be confirmed / not found


class SourceSummary(_Frozen):
    document_key: str
    title: str
    date_basis: str
    scanned: bool


class Diagnostics(_Frozen):
    dropped: tuple[tuple[str, str], ...] = ()  # (item id, reason)
    retrieval_degraded: bool = False
    retrieved_citations: int = 0


class ChecklistAnswer(_Frozen):
    kind: AnswerKind
    intent: Intent
    customer_category: CustomerCategory | None
    account_type: AccountType | None
    coverage_state: CoverageState
    facets: dict[Slot, str | None]
    groups: ChecklistGroups | None = None
    fallback: str | None = None
    reason: str | None = None
    technical_message: str | None = None
    notice: str
    coverage_note: str | None = None  # checklists only
    sources: tuple[SourceSummary, ...] = ()
    diagnostics: Diagnostics = Field(default_factory=Diagnostics, exclude=True)


# --- the service ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    profile: ReadyProfile
    scope: str  # "opening" | "conversion"
    state: CoverageState
    sources: tuple[str, ...]
    reason: str | None = None


@dataclass
class _Build:
    dropped: list[tuple[str, str]] = field(default_factory=list)
    retrieval_degraded: bool = False
    retrieved: int = 0


class ChecklistService:
    """Builds one checklist. `retriever` is optional: without it, evidence comes only from the
    catalogue's cited chunks (still re-validated in Postgres)."""

    def __init__(
        self,
        chunks: ChunkSource,
        retriever: AssistantRetriever | None = None,
        *,
        cov: Coverage | None = None,
        cat: Catalogue | None = None,
    ) -> None:
        self.chunks = chunks
        self.retriever = retriever
        self.cov = cov or coverage()
        self.cat = cat or catalogue()
        self.words = self.cat.wording

    # 1. validate -------------------------------------------------------------------------------

    def validate(self, profile: ReadyProfile) -> _Plan:
        for t in profile.topics:
            if t not in {x.value for x in Topic}:
                raise InvalidProfile(f"unknown topic {t!r}")
        if profile.intent is Intent.RESIDENT_TO_NRO_CONVERSION:
            if profile.coverage_state is not CoverageState.CONVERSION_ONLY or profile.facets:
                raise InvalidProfile("a conversion request is CONVERSION_ONLY, without facets")
            return _Plan(profile, "conversion", CoverageState.CONVERSION_ONLY,
                         self.cov.conversion.sources)  # fmt: skip
        cat, acct = profile.customer_category, profile.account_type
        if cat is None or acct is None:
            raise InvalidProfile("an opening request names the category and the account type")
        cell = self.cov.cell(cat, acct)
        if profile.coverage_state is not cell.state:
            raise InvalidProfile(f"coverage state {profile.coverage_state} != map {cell.state}")
        if not cell.covered:
            return _Plan(profile, "opening", cell.state, (), cell.reason)
        material = {slot_for_facet(f) for f in cell.material_facets}
        if set(profile.facets) != material:
            raise InvalidProfile("facets must be exactly the cell's material facets")
        for slot, value in profile.facets.items():
            if value is not None and not valid_value(slot, value):
                raise InvalidProfile(f"invalid value {value!r} for {slot}")
        return _Plan(profile, "opening", cell.state, cell.sources)

    # entry point -------------------------------------------------------------------------------

    def build(self, profile: ReadyProfile) -> ChecklistAnswer:
        plan = self.validate(profile)
        if not plan.sources:
            return self._answer(plan, AnswerKind.FALLBACK, reason=plan.reason)
        b = _Build()
        items = [i for i in self.cat.items if self._applies(i, plan)]
        entries = [e for e in self.cat.not_confirmed if self._entry_applies(e, plan)]
        hits = self._retrieve(items, plan, b)
        wanted = {ev.chunk_id for i in items for ev in i.evidence}
        wanted |= {cid for ids in hits.values() for cid in ids}
        try:
            stored = self.chunks.get(wanted)
        except Exception:  # storage failure is not "no evidence" (A1 §7)
            log.exception("checklist: stored evidence could not be read")
            return self._answer(plan, AnswerKind.TECHNICAL_ERROR)
        built: dict[str, ChecklistItem] = {}
        for item in items:
            out = self._build_item(item, plan, stored, hits.get(item.id, ()), b)
            if out is not None:
                built[item.id] = out
        if not built:
            return self._answer(plan, AnswerKind.FALLBACK, diagnostics=b)
        asked = set(profile.topics) - {Topic.DOCUMENTS.value}
        groups = self._assemble(built, entries, asked, plan)
        return self._answer(plan, AnswerKind.CHECKLIST, groups=groups, diagnostics=b)

    # 2. select ---------------------------------------------------------------------------------

    def _applies(self, item: CatalogueItem, plan: _Plan) -> bool:
        p = plan.profile
        if item.scope != plan.scope:
            return False
        if plan.scope == "opening":
            if p.customer_category not in item.categories:
                return False
            if item.accounts is not None and p.account_type not in item.accounts:
                return False
        for slot, values in item.when.items():
            value = p.facets.get(slot)
            if value is not None and value not in values:
                return False  # a known, different value: the section does not apply
        asked = {t.value for t in item.topics} & set(p.topics)
        return not item.only_when_asked or bool(asked)

    def _entry_applies(self, entry: NotConfirmedEntry, plan: _Plan) -> bool:
        """Group C presentation rule (owner, A4 review): a "could not be confirmed" entry is
        shown only when the customer explicitly asked about one of its topics. It is never
        shown proactively. Which entries exist, and what they say, is unchanged."""
        if entry.scope != plan.scope:
            return False
        if not {t.value for t in entry.topics} & set(plan.profile.topics):
            return False
        if plan.scope == "conversion":
            return True
        cats = entry.categories
        if entry.ruling is not None:
            ruling = self.cov.ruling(entry.ruling)
            cats = ruling.categories if ruling else ()
        return plan.profile.customer_category in cats

    def _applies_if(self, item: CatalogueItem, plan: _Plan) -> tuple[str, ...]:
        notes = []
        for slot, values in item.when.items():
            if plan.profile.facets.get(slot) is None:  # unknown, skipped or not asked
                notes += [self.words.applies_if[f"{slot.value}.{v}"] for v in values]
        return tuple(notes)

    # 3. gather ---------------------------------------------------------------------------------

    def _retrieve(
        self, items: list[CatalogueItem], plan: _Plan, b: _Build
    ) -> dict[str, tuple[str, ...]]:
        if self.retriever is None:
            return {}
        cat, acct = plan.profile.customer_category, plan.profile.account_type
        if plan.scope == "opening" and (cat is None or acct is None):
            return {}  # unreachable after `validate`; never search without a covered cell
        out: dict[str, tuple[str, ...]] = {}
        try:
            for item in items:
                if cat is None or acct is None:
                    r = self.retriever.conversion(item.title, top_k=RETRIEVAL_TOP_K)
                else:
                    r = self.retriever.opening(item.title, cat, acct, top_k=RETRIEVAL_TOP_K)
                out[item.id] = tuple(h.chunk_id for h in r.hits)
        except AssistantScopeError:
            log.error("checklist: retrieval returned an out-of-scope chunk; retrieval ignored")
            b.retrieval_degraded = True
            return {}
        except Exception:  # retrieval only adds references; the cited evidence still stands
            log.warning("checklist: retrieval unavailable; cited evidence only", exc_info=True)
            b.retrieval_degraded = True
            return {}
        return out

    # 4. resolve and validate -------------------------------------------------------------------

    def _valid(self, chunk: StoredChunk | None, plan: _Plan) -> bool:
        return (
            chunk is not None
            and chunk.bank == ASSISTANT_BANK
            and chunk.document_key in plan.sources
            and self.cov.role(chunk.document_key)
            in (SourceRole.OPENING_FORM, SourceRole.CONVERSION_FORM, SourceRole.POLICY)
        )

    def _citation(self, chunk: StoredChunk, *, basis: bool = True) -> Citation:
        doc = self.cov.documents[chunk.document_key]
        return Citation(
            chunk_id=chunk.chunk_id,
            document_key=chunk.document_key,
            document_title=doc.title,
            page=chunk.page,
            page_end=chunk.page_end,
            section=chunk.section,
            text_reliability=chunk.text_reliability,
            extraction_method=chunk.extraction_method,
            date_basis=doc.date_basis,
            quotable=chunk.text_reliability in QUOTABLE,
            basis=basis,
            passage=chunk.content,
        )

    def _build_item(
        self,
        item: CatalogueItem,
        plan: _Plan,
        stored: dict[str, StoredChunk],
        hit_ids: tuple[str, ...],
        b: _Build,
    ) -> ChecklistItem | None:
        cited: dict[str, Citation] = {}  # one citation per chunk, however often it is cited
        quote_of: dict[str, str] = {}
        for ev in item.evidence:
            chunk = stored.get(ev.chunk_id)
            if chunk is None:
                b.dropped.append((item.id, f"chunk {ev.chunk_id} not stored"))
                continue
            if not self._valid(chunk, plan):
                b.dropped.append((item.id, f"chunk {ev.chunk_id} not a permitted source"))
                continue
            if chunk.document_key != ev.document_key or chunk.page != ev.page:
                b.dropped.append((item.id, f"chunk {ev.chunk_id} document or page mismatch"))
                continue
            if ev.quote not in chunk.content:
                b.dropped.append((item.id, f"quote not in chunk {ev.chunk_id}"))
                continue
            if ev.chunk_id not in cited:
                cited[ev.chunk_id] = self._citation(chunk)
                quote_of[ev.chunk_id] = ev.quote
        if not cited:
            return None
        if item.condition is not None and not any(item.condition in q for q in quote_of.values()):
            b.dropped.append((item.id, "condition not in the validated evidence"))
            return None
        quotes = [
            ev.quote
            for ev in item.evidence
            if ev.chunk_id in cited and len(ev.quote) >= MIN_SUPPLEMENTARY_QUOTE
        ]
        extra = []
        for cid in hit_ids:  # retrieval: more places with the same wording, never new items
            chunk = stored.get(cid)
            if cid in cited or chunk is None or not self._valid(chunk, plan):
                continue
            if any(q in chunk.content for q in quotes):
                extra.append(self._citation(chunk, basis=False))
        extra.sort(key=lambda c: (c.document_key, c.page, c.chunk_id))
        b.retrieved += len(extra)
        citations = tuple(cited.values()) + tuple(extra)

        basis = citations[0]
        cls = classify(self.cov.role(basis.document_key))
        reliable = all(c.quotable for c in cited.values())
        quote = quote_of[basis.chunk_id]
        texts = self.words.caveats
        dated = texts.dated_source.format(date_basis=basis.date_basis)
        caveats = [Caveat(code=CaveatCode.DATED_SOURCE, text=dated)]
        if not reliable or self.cov.documents[basis.document_key].scanned:
            caveats.append(
                Caveat(code=CaveatCode.SCANNED_TEXT, text=self.words.caveats.scanned_text)
            )
        if item.condition is not None:
            conditional = texts.conditional.format(condition=item.condition)
            caveats.append(Caveat(code=CaveatCode.CONDITIONAL, text=conditional))
        return ChecklistItem(
            id=item.id,
            title=item.title,
            kind=item.kind,
            classification=cls,
            classification_label=self.words.classification_labels[cls.value],
            condition=item.condition,
            applies_if=self._applies_if(item, plan),
            source_wording=quote if reliable else None,
            approximate_reading=None if reliable else quote,
            uncertainty_warning=None if reliable else self.words.ocr_warning,
            establishes=getattr(self.words.establishes, item.kind),
            does_not_establish=self.words.does_not_establish,
            note=item.note,
            citations=citations,
            caveats=tuple(caveats),
            asked=bool({t.value for t in item.topics} & set(plan.profile.topics)),
        )

    # 5. assemble -------------------------------------------------------------------------------

    def _assemble(
        self,
        built: dict[str, ChecklistItem],
        entries: list[NotConfirmedEntry],
        asked: set[str],
        plan: _Plan,
    ) -> ChecklistGroups:
        rank = {Classification.POLICY_SUPPORTED: 0, Classification.FORM_INDICATED: 1}
        docs = [i for i in built.values() if i.kind in ("document", "declaration")]
        docs.sort(key=lambda i: rank.get(i.classification, 2))  # stable: keeps the form order
        info = [i for i in built.values() if i.kind == "information"]
        answered = {t for i in built.values() for t in self._topics(i.id)}
        not_confirmed: list[ChecklistItem] = []
        for e in entries:
            reason = e.reason
            if e.ruling is not None:
                ruling = self.cov.ruling(e.ruling)
                reason = ruling.reason if ruling else None
            related = tuple(c for rid in e.related if rid in built for c in built[rid].citations)
            not_confirmed.append(ChecklistItem(
                id=e.id,
                title=e.title,
                kind="document",
                classification=Classification.UNCONFIRMED,
                classification_label=self.words.classification_labels["unconfirmed"],
                does_not_establish=reason or self.words.unconfirmed_explanation,
                related_citations=related,
                asked=bool({t.value for t in e.topics} & asked),
            ))  # fmt: skip
            answered |= {t.value for t in e.topics}
        for topic in sorted(asked - answered):
            not_confirmed.append(ChecklistItem(
                id=f"unknown_{topic}",
                title=self.words.unknown_topics[Topic(topic)],
                kind="document",
                classification=Classification.UNKNOWN,
                classification_label=self.words.classification_labels["unknown"],
                does_not_establish=self.words.unconfirmed_explanation,
                asked=True,
                fallback=self.words.fallback,
            ))  # fmt: skip
        return ChecklistGroups(
            documents=tuple(docs), information=tuple(info), not_confirmed=tuple(not_confirmed)
        )

    def _topics(self, item_id: str) -> set[str]:
        item = self.cat.item(item_id)
        return {t.value for t in item.topics} if item else set()

    def _answer(
        self,
        plan: _Plan,
        kind: AnswerKind,
        *,
        groups: ChecklistGroups | None = None,
        reason: str | None = None,
        diagnostics: _Build | None = None,
    ) -> ChecklistAnswer:
        p = plan.profile
        d = diagnostics or _Build()
        sources = tuple(
            SourceSummary(document_key=k, title=self.cov.documents[k].title,
                          date_basis=self.cov.documents[k].date_basis,
                          scanned=self.cov.documents[k].scanned)  # fmt: skip
            for k in plan.sources
        )
        return ChecklistAnswer(
            kind=kind,
            intent=p.intent,
            customer_category=p.customer_category,
            account_type=p.account_type,
            coverage_state=plan.state,
            facets=dict(p.facets),
            groups=groups,
            fallback=self.words.fallback if kind is AnswerKind.FALLBACK else None,
            reason=reason,
            technical_message=(
                self.words.technical_error if kind is AnswerKind.TECHNICAL_ERROR else None
            ),
            notice=self.words.notice,
            coverage_note=self.words.coverage_note if kind is AnswerKind.CHECKLIST else None,
            sources=sources if kind is AnswerKind.CHECKLIST else (),
            diagnostics=Diagnostics(
                dropped=tuple(d.dropped),
                retrieval_degraded=d.retrieval_degraded,
                retrieved_citations=d.retrieved,
            ),
        )
