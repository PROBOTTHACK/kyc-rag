"""The checklist item catalogue (A4; `checklist.yaml`), validated against the coverage map.

Loading fails closed if any item could cite a source the coverage map does not permit for
it (re-KYC, legacy, not-used, another category's form, the conversion form outside the
conversion request), if a condition is not verbatim source text, or if an item backed by
an A2 item ruling disagrees with that ruling. The catalogue carries no classification:
classes are assigned by `checklist.classify` from the source role.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant.coverage import Coverage, coverage
from app.assistant.customer_text import INTERNAL, check_customer_text
from app.assistant.profile import FACET_SLOTS, Slot, valid_value
from app.assistant.taxonomy import AccountType, CustomerCategory, EvidenceScope, SourceRole
from app.assistant.understand import Topic

CATALOGUE_FILE = Path(__file__).with_name("checklist.yaml")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Customer-facing catalogue text never mentions internal stages, rules or implementation
# (owner, A4 language pass). Checked on load for titles, notes, condition notes, explanations.
_INTERNAL = INTERNAL
_customer_text = check_customer_text


ItemKind = Literal["document", "information", "declaration"]
Scope = Literal["opening", "conversion"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogueEvidence(_Model):
    chunk_id: str = Field(pattern=_UUID)
    document_key: str
    page: int = Field(ge=1)
    quote: str = Field(min_length=1)  # must occur exactly in the stored chunk text


class CatalogueItem(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    kind: ItemKind
    scope: Scope
    categories: tuple[CustomerCategory, ...] = ()
    accounts: tuple[AccountType, ...] | None = None
    when: dict[Slot, tuple[str, ...]] = Field(default_factory=dict)
    condition: str | None = None  # verbatim source text (validated against the quote)
    condition_note: str | None = None  # the same condition in plain customer language
    topics: tuple[Topic, ...] = ()
    only_when_asked: bool = False
    note: str | None = None
    ruling: str | None = None
    evidence: tuple[CatalogueEvidence, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> CatalogueItem:
        if self.scope == "opening" and not self.categories:
            raise ValueError(f"{self.id}: an opening item names its categories")
        if self.scope == "conversion" and (self.categories or self.accounts):
            raise ValueError(f"{self.id}: a conversion item applies to the conversion only")
        for slot, values in self.when.items():
            if slot not in FACET_SLOTS or not values:
                raise ValueError(f"{self.id}: `when` uses facet slots only")
            if not all(valid_value(slot, v) for v in values):
                raise ValueError(f"{self.id}: invalid value for {slot}")
        if self.condition is not None and not any(self.condition in e.quote for e in self.evidence):
            raise ValueError(f"{self.id}: the condition is not verbatim text of its evidence")
        if self.only_when_asked and not self.topics:
            raise ValueError(f"{self.id}: an on-request item names the topics that ask for it")
        if self.condition_note is not None and self.condition is None:
            raise ValueError(f"{self.id}: a condition note explains a verbatim condition")
        _customer_text(self.id, self.title, self.note, self.condition_note)
        return self


class NotConfirmedEntry(_Model):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1)
    scope: Scope
    categories: tuple[CustomerCategory, ...] = ()  # from the ruling when `ruling` is set
    topics: tuple[Topic, ...] = Field(min_length=1)
    related: tuple[str, ...] = ()
    ruling: str | None = None
    reason: str | None = None  # internal rationale (review only; never shown)
    explanation: str = Field(min_length=1)  # what the customer reads

    @model_validator(mode="after")
    def _shape(self) -> NotConfirmedEntry:
        _customer_text(self.id, self.title, self.explanation)
        if (self.ruling is None) == (self.reason is None):
            raise ValueError(f"{self.id}: give either the ruling or the reason, not both")
        if self.ruling is not None and self.categories:
            raise ValueError(f"{self.id}: categories come from the ruling")
        if self.ruling is None and self.scope == "opening" and not self.categories:
            raise ValueError(f"{self.id}: an opening entry names its categories")
        return self


class CaveatWording(_Model):
    dated_source: str
    scanned_text: str
    conditional: str


class EstablishesWording(_Model):
    document: str
    declaration: str
    information: str


class ChecklistWording(_Model):
    classification_labels: dict[str, str]
    establishes: EstablishesWording
    does_not_establish: str
    unconfirmed_explanation: str
    ocr_warning: str
    caveats: CaveatWording
    applies_if: dict[str, str]
    unknown_topics: dict[Topic, str]
    coverage_note: str
    fallback: str
    notice: str
    technical_error: str

    @model_validator(mode="after")
    def _complete(self) -> ChecklistWording:
        codes = {"policy_supported", "form_indicated", "unconfirmed", "legacy_reference", "unknown"}
        if set(self.classification_labels) != codes:
            raise ValueError("a label for every classification code (A1 §3)")
        missing = {t for t in Topic if t is not Topic.DOCUMENTS} - set(self.unknown_topics)
        if missing:
            raise ValueError(f"no wording for topics {sorted(missing)}")
        expected = {f"{s.value}.{v}" for s in FACET_SLOTS for v in _values(s)}
        if set(self.applies_if) != expected:
            raise ValueError("an applies-if note for every facet value")
        _customer_text("wording", *_strings(self.model_dump()))
        return self


def _strings(value: object) -> list[str]:
    """Every string value in nested wording (dicts are walked by value, not by key)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def _values(slot: Slot) -> list[str]:
    from app.assistant.profile import SLOT_VALUES

    return [v.value for v in SLOT_VALUES[slot]]


class Catalogue(_Model):
    version: str
    basis: str
    items: tuple[CatalogueItem, ...]
    not_confirmed: tuple[NotConfirmedEntry, ...]
    wording: ChecklistWording

    def item(self, item_id: str) -> CatalogueItem | None:
        return next((i for i in self.items if i.id == item_id), None)


def validate_against(cat: Catalogue, cov: Coverage) -> None:
    """Cross-checks with the coverage map. Raises ValueError on the first violation."""
    ids = [i.id for i in cat.items] + [n.id for n in cat.not_confirmed]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate catalogue id")
    for item in cat.items:
        _check_item(item, cov)
    referenced: list[str] = [i.ruling for i in cat.items if i.ruling]
    for entry in cat.not_confirmed:
        for rel in entry.related:
            if cat.item(rel) is None:
                raise ValueError(f"{entry.id}: related item {rel} does not exist")
        if entry.ruling is not None:
            ruling = cov.ruling(entry.ruling)
            if ruling is None or ruling.status != "not_established":
                raise ValueError(f"{entry.id}: must reference a not-established ruling")
            referenced.append(entry.ruling)
    if sorted(referenced) != sorted(r.id for r in cov.item_rulings):
        raise ValueError("every A2 item ruling is used exactly once")


def _check_item(item: CatalogueItem, cov: Coverage) -> None:
    allowed_role = (
        SourceRole.OPENING_FORM if item.scope == "opening" else SourceRole.CONVERSION_FORM
    )
    for ev in item.evidence:
        doc = cov.documents.get(ev.document_key)
        if doc is None or doc.role is not allowed_role:
            raise ValueError(f"{item.id}: {ev.document_key} is not a permitted {item.scope} source")
    if item.scope == "conversion":
        if any(ev.document_key not in cov.conversion.sources for ev in item.evidence):
            raise ValueError(f"{item.id}: not a source of the conversion request")
    else:
        for c in item.categories:
            accounts = item.accounts or tuple(a for a in AccountType if cov.cell(c, a).covered)
            for a in accounts:
                sources = cov.documents_for(EvidenceScope.OPENING, c, a)
                if not sources:
                    raise ValueError(f"{item.id}: {c}/{a} is not a covered combination")
                if any(ev.document_key not in sources for ev in item.evidence):
                    raise ValueError(f"{item.id}: evidence is not a source of {c}/{a}")
    if item.ruling is not None:
        ruling = cov.ruling(item.ruling)
        if ruling is None or ruling.status != "evidenced":
            raise ValueError(f"{item.id}: must reference an evidenced ruling")
        same = (
            ruling.kind == item.kind
            and set(ruling.categories) == set(item.categories)
            and ruling.condition == item.condition
            and {e.chunk_id for e in ruling.evidence} == {e.chunk_id for e in item.evidence}
        )
        if not same:
            raise ValueError(f"{item.id}: disagrees with A2 ruling {ruling.id}")


def load_catalogue(path: Path = CATALOGUE_FILE, cov: Coverage | None = None) -> Catalogue:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("catalogue file is not a mapping")
    cat = Catalogue.model_validate(raw)
    validate_against(cat, cov or coverage())
    return cat


@lru_cache(maxsize=1)
def catalogue() -> Catalogue:
    return load_catalogue()
