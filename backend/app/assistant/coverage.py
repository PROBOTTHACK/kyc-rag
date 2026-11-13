"""The evidence-based coverage map (A2; A1 §5.3; A0 §2).

Loaded from `coverage.yaml` and validated on load: a map that would create coverage
without evidence, use a re-KYC / legacy / RBI source, or mix scanned and native states
fails to load (fail closed). Any (category, account type) not listed is NOT_ESTABLISHED.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.taxonomy import (
    SCOPE_ROLES,
    AccountType,
    CoverageState,
    CustomerCategory,
    EvidenceScope,
    Facet,
    Intent,
    SourceRole,
)

COVERAGE_FILE = Path(__file__).with_name("coverage.yaml")
DEFAULT_NOT_ESTABLISHED = (
    "No applicable HDFC account opening material for this combination exists in the corpus."
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceDocument(_Model):
    role: SourceRole
    title: str = Field(min_length=1)
    categories: tuple[CustomerCategory, ...]
    scanned: bool
    date_basis: str = Field(min_length=1)


class SchemaRef(_Model):
    form_key: str
    extraction_method: Literal["m1_chunks", "gemini_multimodal"]
    extractor_version: int = Field(ge=1)
    label: str = Field(min_length=1)


class Evidence(_Model):
    chunk_id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    document_key: str
    page: int = Field(ge=1)
    basis: Literal["stored_text", "schema_field"]
    shows: str = Field(min_length=1)
    schema_ref: SchemaRef | None = Field(default=None, alias="schema")
    corroborated: bool | None = None

    @model_validator(mode="after")
    def _schema_consistent(self) -> Evidence:
        if (self.basis == "schema_field") != (self.schema_ref is not None):
            raise ValueError("schema_field evidence names its schema field, and only it does")
        if self.basis == "schema_field" and self.corroborated is None:
            raise ValueError("schema_field evidence states whether it is corroborated")
        return self


class FacetEvidence(_Model):
    facet: Facet
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class Cell(_Model):
    category: CustomerCategory
    account_type: AccountType
    state: CoverageState
    sources: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    facets: tuple[FacetEvidence, ...] = ()
    reason: str | None = None  # only for NOT_ESTABLISHED

    @property
    def covered(self) -> bool:
        return self.state in (CoverageState.COVERED, CoverageState.COVERED_OCR)

    @property
    def material_facets(self) -> tuple[Facet, ...]:
        return tuple(f.facet for f in self.facets)


class Conversion(_Model):
    intent: Intent
    state: CoverageState
    sources: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = Field(min_length=1)


class NotEstablishedReason(_Model):
    category: CustomerCategory | None
    account_type: AccountType | None
    reason: str = Field(min_length=1)


class ItemRuling(_Model):
    """An owner-approved statement of what the corpus does or does not establish for one
    item (A1 §5.4). It records evidence only, never an evidence classification."""

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    subject: str = Field(min_length=1)
    kind: Literal["document", "information"]
    status: Literal["evidenced", "not_established"]
    categories: tuple[CustomerCategory, ...] = Field(min_length=1)
    sources: tuple[str, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = ()
    condition: str | None = None  # the source's own wording, verbatim
    reason: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ItemRuling:
        if self.status == "not_established":
            if self.evidence or self.condition is not None:
                raise ValueError(f"{self.id}: a not-established item has no evidence or condition")
            if not self.reason:
                raise ValueError(f"{self.id}: a not-established item states why")
        elif not self.evidence:
            raise ValueError(f"{self.id}: an evidenced item cites stored evidence")
        return self


class Coverage(_Model):
    version: str
    basis: str
    documents: dict[str, SourceDocument]
    cells: tuple[Cell, ...]
    conversion: Conversion
    not_established_reasons: tuple[NotEstablishedReason, ...] = ()
    item_rulings: tuple[ItemRuling, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Coverage:
        seen: set[tuple[CustomerCategory, AccountType]] = set()
        for cell in self.cells:
            key = (cell.category, cell.account_type)
            if key in seen:
                raise ValueError(f"duplicate cell {key}")
            seen.add(key)
            self._check_cell(cell)
        conv = self.conversion
        if conv.intent is not Intent.RESIDENT_TO_NRO_CONVERSION:
            raise ValueError("the conversion entry is the resident-to-NRO intent")
        if conv.state is not CoverageState.CONVERSION_ONLY:
            raise ValueError("the conversion intent is CONVERSION_ONLY")
        for doc_key in conv.sources:
            if self._doc(doc_key).role is not SourceRole.CONVERSION_FORM:
                raise ValueError(f"conversion source {doc_key} is not a conversion form")
        self._check_evidence(conv.evidence, conv.sources, "conversion")
        ids = [r.id for r in self.item_rulings]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate item ruling")
        for r in self.item_rulings:
            for doc_key in r.sources:
                doc = self._doc(doc_key)
                if doc.role not in SCOPE_ROLES[EvidenceScope.OPENING]:
                    raise ValueError(f"{r.id}: {doc_key} ({doc.role}) is not opening material")
                if not set(r.categories) <= set(doc.categories):
                    raise ValueError(f"{r.id}: {doc_key} does not cover every listed category")
            self._check_evidence(r.evidence, r.sources, r.id)
        return self

    def _doc(self, key: str) -> SourceDocument:
        if key not in self.documents:
            raise ValueError(f"unknown document {key}")
        return self.documents[key]

    def _check_cell(self, cell: Cell) -> None:
        where = f"{cell.category}/{cell.account_type}"
        if not cell.covered:
            raise ValueError(f"{where}: list only covered cells; others are NOT_ESTABLISHED")
        if cell.reason is not None:
            raise ValueError(f"{where}: a covered cell has no not-established reason")
        if not cell.sources or not cell.evidence:
            raise ValueError(f"{where}: a covered cell needs sources and evidence")
        scanned = []
        for key in cell.sources:
            doc = self._doc(key)
            if doc.role not in SCOPE_ROLES[EvidenceScope.OPENING]:
                raise ValueError(f"{where}: {key} ({doc.role}) cannot establish opening coverage")
            if cell.category not in doc.categories:
                raise ValueError(f"{where}: {key} does not cover {cell.category}")
            scanned.append(doc.scanned)
        expected = CoverageState.COVERED_OCR if any(scanned) else CoverageState.COVERED
        if cell.state is not expected:
            raise ValueError(f"{where}: state must be {expected} for these sources")
        self._check_evidence(cell.evidence, cell.sources, where)
        facets = [f.facet for f in cell.facets]
        if len(set(facets)) != len(facets):
            raise ValueError(f"{where}: duplicate facet")
        for f in cell.facets:
            self._check_evidence(f.evidence, cell.sources, f"{where}/{f.facet}")

    def _check_evidence(
        self, evidence: tuple[Evidence, ...], sources: tuple[str, ...], where: str
    ) -> None:
        for ev in evidence:
            if ev.document_key not in sources:
                raise ValueError(f"{where}: evidence from {ev.document_key} is not a cell source")
            if ev.schema_ref is not None and ev.schema_ref.form_key != ev.document_key:
                raise ValueError(f"{where}: schema evidence names another form")

    # --- queries -------------------------------------------------------------------------------

    def cell(self, category: CustomerCategory, account_type: AccountType) -> Cell:
        for c in self.cells:
            if c.category is category and c.account_type is account_type:
                return c
        return Cell(
            category=category,
            account_type=account_type,
            state=CoverageState.NOT_ESTABLISHED,
            reason=self.not_established_reason(category, account_type),
        )

    def not_established_reason(self, category: CustomerCategory, account_type: AccountType) -> str:
        for r in self.not_established_reasons:  # most specific first: category, then account
            if r.category is category and r.account_type in (None, account_type):
                return r.reason
        for r in self.not_established_reasons:
            if r.category is None and r.account_type is account_type:
                return r.reason
        return DEFAULT_NOT_ESTABLISHED

    def role(self, document_key: str) -> SourceRole | None:
        doc = self.documents.get(document_key)
        return doc.role if doc else None

    def documents_for(
        self,
        scope: EvidenceScope,
        category: CustomerCategory | None = None,
        account_type: AccountType | None = None,
    ) -> tuple[str, ...]:
        """The only document keys a retrieval in `scope` may read. Empty means: retrieve
        nothing (e.g. an opening query for a NOT_ESTABLISHED cell)."""
        if scope is EvidenceScope.OPENING:
            if category is None or account_type is None:
                return ()
            cell = self.cell(category, account_type)
            return cell.sources if cell.covered else ()
        if scope is EvidenceScope.CONVERSION:
            return self.conversion.sources
        roles = SCOPE_ROLES[scope]
        return tuple(
            sorted(
                key
                for key, doc in self.documents.items()
                if doc.role in roles and (category is None or category in doc.categories)
            )
        )

    def all_evidence(self) -> tuple[Evidence, ...]:
        out: list[Evidence] = []
        for c in self.cells:
            out += c.evidence
            for f in c.facets:
                out += f.evidence
        out += self.conversion.evidence
        for r in self.item_rulings:
            out += r.evidence
        return tuple(out)

    def ruling(self, ruling_id: str) -> ItemRuling | None:
        return next((r for r in self.item_rulings if r.id == ruling_id), None)

    def rulings_for(self, category: CustomerCategory) -> tuple[ItemRuling, ...]:
        return tuple(r for r in self.item_rulings if category in r.categories)


def load_coverage(path: Path = COVERAGE_FILE) -> Coverage:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("coverage file is not a mapping")
    raw.pop("_evidence", None)  # anchors only
    return Coverage.model_validate(raw)


@lru_cache(maxsize=1)
def coverage() -> Coverage:
    return load_coverage()
