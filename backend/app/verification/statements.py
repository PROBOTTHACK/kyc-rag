"""Deterministic statement analysis of one evidence window (M4 S2; M4_PLAN §E).

`analyse` reads a window of stored evidence text and reports, as codes:
- modality: obligation (shall, must, is required, mandatory, ...), permission (may,
  optional, ...), conditional (an obligation qualified by if / where / in case / subject
  to / where applicable ...), or none;
- polarity: affirmative, negated (shall not, need not, not required, exempt, dispensed
  with, waived, ...), or ambiguous (double negation, an exception such as "except" or
  "other than", or a negator that does not attach to the obligation);
- object_match: whether the item's anchor terms occur in the window (see `anchors`);
- subject: who is obliged, when the text names it (never inferred);
- scope: whether the window speaks of the customer's entity type, of another one only,
  or of none.

It is a NECESSARY condition only. No keyword ever establishes MANDATORY: the decision table
also needs model entailment above a dev-selected threshold and an eligible primary source
(state.mandatory_established). Ambiguity always resolves to "not supported", never to
"supported" and never to "not required".

Known limits (measured in S3/S4, not patched over): clause structure is approximated by
cue words, synonyms are not expanded (no invented equivalences), and a cue in a different
clause from the item can still be read as applying to it; the window construction keeps
windows short to limit that.
"""

import re
from dataclasses import dataclass
from typing import Literal

from app.graph.state import StatementAnalysis
from app.schemas.taxonomy import EntityType

# --- modality and polarity cues (word-bounded, case-insensitive) ---------------------------

_OBLIGATION = {
    "shall": r"\bshall\b(?!\s+not\b)",
    "must": r"\bmust\b(?!\s+not\b)",
    "required": r"\b(?:is|are|be|being|been)\s+required\b|\brequired\s+to\b",
    "requires": r"\brequires?\b(?!\s+not\b)",
    "mandatory": r"\bmandator(?:y|ily)\b",
    "compulsory": r"\bcompulsor(?:y|ily)\b",
    "obligatory": r"\bobligatory\b",
    "need_to": r"\b(?:needs?|has|have)\s+to\b",
    "necessary": r"\b(?:is|are)\s+necessary\b",
}
_PERMISSION = {
    "may": r"\bmay\b",
    "can": r"\bcan\b",
    "optional": r"\boptional(?:ly)?\b",
    "at_option": r"\bat\s+(?:the|his|her|its|their)\s+option\b",
    "permitted": r"\b(?:is|are)\s+(?:permitted|allowed)\b",
}
_CONDITION = {
    "if": r"\bif\b",
    "where": r"\bwhere(?:ver)?\b(?!\s*as\b)",
    "in_case": r"\bin\s+(?:the\s+)?case\s+of\b|\bin\s+case\b",
    "provided_that": r"\bprovided\s+that\b",
    "subject_to": r"\bsubject\s+to\b",
    "as_applicable": r"\b(?:as|where|if)\s+applicable\b",
    "when": r"\bwhen(?:ever)?\b",
}
# A negation that attaches to the obligation itself.
_MODAL_NEGATION = {
    "shall_not": r"\bshall\s+not\b",
    "must_not": r"\bmust\s+not\b",
    "need_not": r"\bneed\s+not\b",
    "not_required": r"\bnot\s+(?:be\s+)?(?:required|mandatory|compulsory|necessary|needed)\b",
    "no_need": r"\bno\s+(?:need|requirement)\b",
    "not_require": r"\b(?:does|do|did)\s+not\s+require\b",
    "exempt": r"\bexempt(?:ed|ion)?\b",
    "dispensed": r"\bdispensed\s+with\b",
    "waived": r"\bwaived\b",
    "not_applicable": r"\bnot\s+applicable\b",
}
# Exceptions carve out cases: the statement no longer reads as a plain affirmative.
_EXCEPTION = {
    "except": r"\bexcept\b",
    "other_than": r"\bother\s+than\b",
    "excluding": r"\bexcluding\b",
    "unless": r"\bunless\b",
    "save_for": r"\bsave\s+(?:for|as)\b",
}
# Any other negator ("documents not older than", "no account"). "No." / "No " followed by
# a number is a document number ("Form No. 60"), never a negation.
_OTHER_NEGATION = (
    r"\bcannot\b|\bcan't\b|\bnot\b|\bnever\b|\bneither\b|\bnor\b|\bnone\b|\bwithout\b"
    r"|\bno\b(?!\.?\s*\d)(?!\.)"
)


def _found(cues: dict[str, str], text: str) -> list[str]:
    return [name for name, pattern in cues.items() if re.search(pattern, text, re.IGNORECASE)]


def _occurrences(patterns: list[str], text: str) -> int:
    """Distinct places a family of cues occurs: overlapping matches ("shall not be
    required" matches two negation patterns) count once."""
    spans = sorted(
        (m.start(), m.end()) for p in patterns for m in re.finditer(p, text, re.IGNORECASE)
    )
    count, end = 0, -1
    for s, e in spans:
        if s >= end:
            count += 1
        end = max(end, e)
    return count


# --- subjects (who is obliged): recorded only when named -----------------------------------

SUBJECTS: dict[str, str] = {
    "regulated_entity": r"\b(?:the\s+)?(?:REs?|regulated\s+entit(?:y|ies)|bank(?:s)?)\b",
    "customer": r"\b(?:customers?|clients?|account\s+holders?|applicants?)\b",
    "designated_partner": r"\bdesignated\s+partners?\b",
    "partner": r"\b(?<!designated\s)partners?\b",
    "director": r"\bdirectors?\b",
    "trustee": r"\btrustees?\b",
    "beneficial_owner": r"\bbeneficial\s+owners?\b",
    "authorised_signatory": r"\bauthori[sz]ed\s+signator(?:y|ies)\b",
    "karta": r"\bkarta\b",
    "proprietor": r"\bproprietors?\b",
    "employee": r"\bemployees?\b",
}
# Natural persons tied to an entity: two different ones are different subjects.
PERSON_ROLES: frozenset[str] = frozenset(
    {"designated_partner", "partner", "director", "trustee", "beneficial_owner",
     "authorised_signatory", "karta", "proprietor", "employee"}
)  # fmt: skip


def subjects_in(text: str) -> list[str]:
    return [name for name, p in SUBJECTS.items() if re.search(p, text, re.IGNORECASE)]


def _subject(text: str) -> str | None:
    """The named party nearest before the first obligation cue, else the first named."""
    cue = min(
        (m.start() for p in _OBLIGATION.values() for m in re.finditer(p, text, re.IGNORECASE)),
        default=None,
    )
    best: tuple[int, str] | None = None
    for name, p in SUBJECTS.items():
        for m in re.finditer(p, text, re.IGNORECASE):
            if cue is not None and m.start() > cue:
                continue
            if best is None or m.start() > best[0]:
                best = (m.start(), name)
    if best is not None:
        return best[1]
    named = subjects_in(text)
    return named[0] if named else None


# --- entity scope ---------------------------------------------------------------------------

ENTITY_TERMS: dict[EntityType, str] = {
    EntityType.INDIVIDUAL: r"\bindividuals?\b",
    EntityType.SOLE_PROPRIETORSHIP: r"\bsole\s+proprietor\w*|\bproprietary\s+concerns?\b",
    EntityType.PARTNERSHIP: r"\b(?<!liability\s)partnership(?:\s+firms?)?\b",
    EntityType.LLP: r"\blimited\s+liability\s+partnerships?\b|\bLLPs?\b",
    EntityType.COMPANY: r"\bcompan(?:y|ies)\b",
    EntityType.TRUST: r"\btrusts?\b",
    EntityType.HUF: r"\bhindu\s+undivided\s+famil(?:y|ies)\b|\bHUFs?\b",
    EntityType.SOCIETY: r"\bsociet(?:y|ies)\b",
    EntityType.UNINCORPORATED_ASSOCIATION: (
        r"\bunincorporated\s+associations?\b|\bassociations?\s+of\s+persons\b"
        r"|\bbod(?:y|ies)\s+of\s+individuals\b"
    ),
    EntityType.OTHER_JURIDICAL_PERSON: r"\bjuridical\s+persons?\b",
}


def entities_in(text: str) -> set[EntityType]:
    return {e for e, p in ENTITY_TERMS.items() if re.search(p, text, re.IGNORECASE)}


Scope = Literal["match", "mismatch", "unknown"]


def _scope(text: str, section: str | None, entity: EntityType | None) -> tuple[Scope, str | None]:
    """match / mismatch / unknown, from the window, else its section heading."""
    if entity is None:
        return "unknown", None
    for source, where in ((text, "window"), (section or "", "section")):
        named = entities_in(source)
        if entity in named:
            return "match", where
        if named:
            return "mismatch", where
    return "unknown", None


# --- anchors (the item the claim is about) --------------------------------------------------

# Generic words that never identify an item on their own.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "by", "with", "as",
        "at", "from", "its", "their", "his", "her", "this", "that", "these", "those", "any",
        "all", "each", "every", "copy", "copies", "certified", "self", "attested",
        "document", "documents", "details", "detail", "number", "numbers", "no", "proof",
        "existing", "new", "customer", "customers", "account", "accounts", "required",
        "mandatory", "is", "are", "be", "must", "shall", "may", "will", "should", "it",
    }
)  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*")


@dataclass(frozen=True)
class Anchors:
    """`strict`: groups of which at least one alternative must occur ("60/61" -> {60, 61});
    numbers and acronyms are strict, so an invented form number or document can never be
    matched. `words`: other content words; at least half must occur."""

    strict: tuple[frozenset[str], ...]
    words: frozenset[str]

    @property
    def empty(self) -> bool:
        return not self.strict and not self.words


def _norm(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


# Words naming the customer's entity type ("Trust deed", "PAN of the firm"): they say whose
# item it is, not which item, so they identify an item only when nothing else does.
ENTITY_WORDS: frozenset[str] = frozenset(
    {"trust", "partnership", "firm", "company", "llp", "huf", "society", "individual",
     "proprietorship", "proprietor", "association", "entity", "concern"}
)  # fmt: skip


def anchors(phrase: str) -> Anchors:
    strict: list[frozenset[str]] = []
    words: set[str] = set()
    for tok in _TOKEN.findall(phrase):
        parts = tok.split("/")
        if any(p.isdigit() for p in parts) or all(p.isupper() and len(p) >= 2 for p in parts):
            strict.append(frozenset(_norm(p) for p in parts))
            continue
        for p in parts:
            if p.lower() not in STOPWORDS and len(p) > 1:
                words.add(_norm(p))
    specific = words - ENTITY_WORDS
    return Anchors(tuple(strict), frozenset(specific if specific or strict else words))


def anchors_missing(spec: Anchors, text: str) -> list[str]:
    """The anchor terms not found in `text` (empty = the item is named)."""
    present = {_norm(t) for tok in _TOKEN.findall(text) for t in tok.split("/")}
    missing = ["/".join(sorted(g)) for g in spec.strict if not g & present]
    if spec.words:
        absent = sorted(spec.words - present)
        if len(absent) * 2 > len(spec.words):  # fewer than half present
            missing += absent
    return missing


# --- the analysis -------------------------------------------------------------------------------


def analyse(
    text: str,
    spec: Anchors,
    *,
    entity: EntityType | None,
    section: str | None = None,
) -> StatementAnalysis:
    cues: list[str] = []
    obligation = _found(_OBLIGATION, text)
    permission = _found(_PERMISSION, text)
    condition = _found(_CONDITION, text)
    modal_neg = _found(_MODAL_NEGATION, text)
    exception = _found(_EXCEPTION, text)
    cues += [f"obl:{c}" for c in obligation] + [f"perm:{c}" for c in permission]
    cues += [f"cond:{c}" for c in condition] + [f"neg:{c}" for c in modal_neg]
    cues += [f"exc:{c}" for c in exception]

    # Other negators: those not already part of a modal negation or a "no need" phrase.
    stripped = text
    for pattern in _MODAL_NEGATION.values():
        stripped = re.sub(pattern, " ", stripped, flags=re.IGNORECASE)
    other_neg = len(re.findall(_OTHER_NEGATION, stripped, re.IGNORECASE))
    if other_neg:
        cues.append("neg:other")

    modality: Literal["obligation", "permission", "conditional", "none"]
    polarity: Literal["affirmative", "negated", "ambiguous"]
    if obligation or (modal_neg and not permission):
        modality = "conditional" if condition else "obligation"
    elif permission:
        modality = "permission"
    elif condition and modal_neg:
        modality = "conditional"
    else:
        modality = "none"

    neg_places = _occurrences(list(_MODAL_NEGATION.values()), text)
    if neg_places + other_neg >= 2 or (modal_neg and exception):
        polarity = "ambiguous"  # double negation, or a negation with an exception
    elif modal_neg:
        polarity = "negated"
    elif exception or other_neg:
        polarity = "ambiguous"  # an exception, or a negator not tied to the obligation
    else:
        polarity = "affirmative"

    scope, where = _scope(text, section, entity)
    if where == "section":
        cues.append("scope:section")
    missing = anchors_missing(spec, text) if not spec.empty else ["no_anchor_terms"]
    subject = _subject(text)
    return StatementAnalysis(
        modality=modality,
        polarity=polarity,
        object_match=not missing,
        subject=subject,
        scope=scope,
        cues=cues[:20],
    )


def subject_conflict(claim_text: str, analysis: StatementAnalysis) -> bool:
    """The claim names a person role and the window obliges a different person role
    (e.g. claim "every employee must submit ID", window "designated partners")."""
    if analysis.subject not in PERSON_ROLES:
        return False
    claimed = set(subjects_in(claim_text)) & PERSON_ROLES
    return bool(claimed) and analysis.subject not in claimed
