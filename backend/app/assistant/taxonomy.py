"""Vocabularies of the assistant (A1 spec §2, §3, §5). Codes are stable identifiers; the
customer-facing wording lives with the UI (A6)."""

from enum import StrEnum


class CustomerCategory(StrEnum):
    RESIDENT_INDIVIDUAL = "RESIDENT_INDIVIDUAL"
    NRI_INDIVIDUAL = "NRI_INDIVIDUAL"
    SOLE_PROPRIETORSHIP = "SOLE_PROPRIETORSHIP"
    PARTNERSHIP_FIRM = "PARTNERSHIP_FIRM"
    LLP = "LLP"
    COMPANY = "COMPANY"
    TRUST = "TRUST"
    HUF = "HUF"
    OTHER_ORGANISATION = "OTHER_ORGANISATION"


class AccountType(StrEnum):
    SAVINGS = "SAVINGS"
    CURRENT = "CURRENT"
    SALARY = "SALARY"
    FIXED_DEPOSIT = "FIXED_DEPOSIT"
    RECURRING_DEPOSIT = "RECURRING_DEPOSIT"
    FCNR_RFC = "FCNR_RFC"


class Intent(StrEnum):
    OPEN_ACCOUNT = "OPEN_ACCOUNT"
    RESIDENT_TO_NRO_CONVERSION = "RESIDENT_TO_NRO_CONVERSION"


class Facet(StrEnum):
    """Follow-up details asked only when material (A1 §6 R3)."""

    NRI_SCHEME = "nri_scheme"  # NRE | NRO
    HOLDING = "holding"  # SINGLE | JOINT
    MINOR = "minor"  # YES | NO


class CoverageState(StrEnum):
    COVERED = "COVERED"
    COVERED_OCR = "COVERED_OCR"
    CONVERSION_ONLY = "CONVERSION_ONLY"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class SourceRole(StrEnum):
    """A1 §2 source hierarchy, per document."""

    POLICY = "policy"  # tier 1: authoritative applicable HDFC policy (none in the corpus)
    OPENING_FORM = "opening_form"  # tier 2
    CONVERSION_FORM = "conversion_form"  # tier 2c: only for the conversion intent
    CONTEXT = "context"  # tier 3: re-KYC / dormant material; never a requirement
    LEGACY_REFERENCE = "legacy_reference"  # tier 4: excluded from checklists
    NOT_USED = "not_used"  # context-only material the assistant never retrieves


class EvidenceScope(StrEnum):
    """Which retrieval path a query uses (A2: opening material kept separate)."""

    OPENING = "opening"
    CONVERSION = "conversion"
    CONTEXT = "context"
    LEGACY = "legacy"


# Which source roles each scope may read. Policy joins the opening scope when it exists.
SCOPE_ROLES: dict[EvidenceScope, frozenset[SourceRole]] = {
    EvidenceScope.OPENING: frozenset({SourceRole.POLICY, SourceRole.OPENING_FORM}),
    EvidenceScope.CONVERSION: frozenset({SourceRole.CONVERSION_FORM}),
    EvidenceScope.CONTEXT: frozenset({SourceRole.CONTEXT}),
    EvidenceScope.LEGACY: frozenset({SourceRole.LEGACY_REFERENCE}),
}

ASSISTANT_BANK = "HDFC"
