"""Deterministic atomic claim extraction (M4 S2; M4_PLAN §D).

What a validated requirement ASSERTS comes from the model's proposed applicability, never
from the evidence:
- MANDATORY   -> an unconditional `policy_obligation`;
- CONDITIONAL -> a conditional `policy_obligation`;
- OPTIONAL / UNCLEAR -> no obligation is asserted (no core claim).
The core claim is always created for MANDATORY/CONDITIONAL, even when no primary source is
cited: the requirement still asserts it, and the verdict must then say it is unsupported.

Weaker, role-specific claims are added for the evidence roles actually cited, each with
its own template and judged ONLY against that role (state.ALLOWED_ROLES):
- form citations      -> `form_request`  ("The form asks for N."): never an obligation;
- supporting citations -> `supporting_statement` (what a dated or non-primary source says).

Explanation sentences become `explanatory_claim`s VERBATIM (model prose already in the
report, never user words), at most `MAX_EXPLANATORY` per item. A sentence longer than
`AtomicClaim.text` allows is not checked and is counted in `skipped`.

Templates are versioned (`TEMPLATES_VERSION`): changing any wording is a new version.
Each template asserts exactly one thing; none uses "requires or requests".
"""

from dataclasses import dataclass
from typing import Literal

from app.graph.state import (
    AtomicClaim,
    ClaimType,
    EvidenceRole,
    ValidatedRequirement,
)
from app.schemas.taxonomy import Applicability, EntityType
from app.verification.statements import analyse, anchors
from app.verification.text import sentences

TEMPLATES_VERSION = "t1"
Strength = Literal["unconditional", "conditional", "none"]
MAX_EXPLANATORY = 6
MAX_CLAIM_CHARS = 300

ENTITY_LABELS: dict[EntityType, str] = {
    EntityType.INDIVIDUAL: "individual",
    EntityType.SOLE_PROPRIETORSHIP: "sole proprietorship",
    EntityType.PARTNERSHIP: "partnership firm",
    EntityType.LLP: "limited liability partnership (LLP)",
    EntityType.COMPANY: "company",
    EntityType.TRUST: "trust",
    EntityType.HUF: "Hindu undivided family (HUF)",
    EntityType.SOCIETY: "society",
    EntityType.UNINCORPORATED_ASSOCIATION: "unincorporated association",
    EntityType.OTHER_JURIDICAL_PERSON: "juridical person",
}


def _customer(entity: EntityType | None) -> str:
    return f"a {ENTITY_LABELS[entity]} customer" if entity is not None else "the customer"


# One proposition per template. {item} is the requirement name (model text, bounded).
TEMPLATES: dict[tuple[ClaimType, str], str] = {
    (ClaimType.POLICY_OBLIGATION, "unconditional"): "{item} must be obtained for {customer}.",
    (ClaimType.POLICY_OBLIGATION, "conditional"): (
        "{item} must be obtained for {customer} in some circumstances."
    ),
    (ClaimType.FORM_REQUEST, "none"): "The form asks for {item}.",
    (ClaimType.SUPPORTING_STATEMENT, "required"): "{item} is listed as needed for {customer}.",
    (ClaimType.SUPPORTING_STATEMENT, "optional"): "{item} is optional for {customer}.",
}


@dataclass(frozen=True)
class Extraction:
    claims: tuple[AtomicClaim, ...]
    skipped: int  # explanation sentences too long to check (counted, never silently lost)


def _text(template: str, item: str, entity: EntityType | None) -> str:
    return template.format(item=item, customer=_customer(entity))[:MAX_CLAIM_CHARS]


def extract(
    req: ValidatedRequirement,
    *,
    entity: EntityType | None,
    roles: set[str],
) -> Extraction:
    """`roles`: the evidence roles of the item's citations (from stored document types)."""
    out: list[AtomicClaim] = []

    def add(
        ctype: ClaimType, strength: Strength, text: str, *, sentence: int | None = None
    ) -> None:
        out.append(
            AtomicClaim(
                claim_id=f"{req.requirement_id}-C{len(out)}",
                requirement_id=req.requirement_id,
                claim_type=ctype,
                asserted_strength=strength,
                text=text,
                template_version=None if sentence is not None else TEMPLATES_VERSION,
                source_sentence_index=sentence,
            )
        )

    proposed = req.proposed_applicability
    if proposed in (Applicability.MANDATORY, Applicability.CONDITIONAL):
        core: Strength = "unconditional" if proposed == Applicability.MANDATORY else "conditional"
        template = TEMPLATES[(ClaimType.POLICY_OBLIGATION, core)]
        add(ClaimType.POLICY_OBLIGATION, core, _text(template, req.name, entity))
    if EvidenceRole.FORM.value in roles:
        template = TEMPLATES[(ClaimType.FORM_REQUEST, "none")]
        add(ClaimType.FORM_REQUEST, "none", _text(template, req.name, entity))
    if EvidenceRole.SUPPORTING_REFERENCE.value in roles and proposed != Applicability.UNCLEAR:
        key = "optional" if proposed == Applicability.OPTIONAL else "required"
        template = TEMPLATES[(ClaimType.SUPPORTING_STATEMENT, key)]
        add(ClaimType.SUPPORTING_STATEMENT, "none", _text(template, req.name, entity))

    skipped = 0
    for index, sentence in enumerate(sentences(req.explanation)):
        if len(out) >= 3 + MAX_EXPLANATORY or index >= MAX_EXPLANATORY:
            skipped += 1
            continue
        if len(sentence) > MAX_CLAIM_CHARS:
            skipped += 1
            continue
        reading = analyse(sentence, anchors(sentence), entity=None)
        strength: Strength = (
            "unconditional"
            if reading.modality == "obligation" and reading.polarity == "affirmative"
            else "conditional" if reading.modality == "conditional" else "none"
        )
        add(ClaimType.EXPLANATORY_CLAIM, strength, sentence, sentence=index)
    return Extraction(tuple(out), skipped)
