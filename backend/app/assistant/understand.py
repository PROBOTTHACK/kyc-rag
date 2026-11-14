"""Deterministic understanding of one customer message (A3; A1 §6 R5, R7).

Rules, not a model. They read the screened message and report:
- **values** stated explicitly and unambiguously, per slot;
- **ambiguous** slots (more than one value mentioned): never filled, asked instead;
- **hints**: context that suggests a value but is not explicit enough to fill it (living
  abroad -> NRI?). A hint becomes a yes/no confirmation question, never a value (R7);
- residency, organisation-without-kind, unsupported customer types, conversion intent,
  item topics, the question kind, out-of-scope subjects, correction cues and control
  replies ("I'm not sure", "skip", "what does this mean?", yes / no).

Vocabulary is customer wording only. It carries no requirement or evidence text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.assistant.profile import Slot
from app.assistant.taxonomy import (
    AccountType,
    CustomerCategory,
    Holding,
    Intent,
    Minor,
    NriScheme,
)


class Residency(StrEnum):
    IN_INDIA = "IN_INDIA"
    OUTSIDE_INDIA = "OUTSIDE_INDIA"


class Topic(StrEnum):
    """What an item question is about. A4 answers it from evidence (or the fallback)."""

    DOCUMENTS = "documents"  # "what do I need?": the checklist itself
    PHOTOGRAPH = "photograph"
    PAN = "pan"
    PAN_CARD_COPY = "pan_card_copy"  # a copy of the card, distinct from the number (A1 §4)
    PAN_NOT_AVAILABLE = "pan_not_available"
    FORM_60 = "form_60"
    PASSPORT = "passport"
    VISA_OR_STATUS_PROOF = "visa_or_status_proof"
    ADDRESS_PROOF = "address_proof"
    IDENTITY_PROOF = "identity_proof"
    AADHAAR = "aadhaar"
    SIGNATORIES = "signatories"
    NOMINEE = "nominee"


class QuestionKind(StrEnum):
    NONE = "none"
    CHECKLIST = "checklist"  # what is needed overall
    ITEM = "item"  # about a specific item
    TERM = "term"  # what a term means / the difference between two terms


class Control(StrEnum):
    NOT_SURE = "not_sure"
    SKIP = "skip"
    EXPLAIN = "explain"
    YES = "yes"
    NO = "no"


class Social(StrEnum):
    """Greetings and small talk: answered naturally, never a profile value or a question."""

    GREETING = "greeting"
    THANKS = "thanks"
    GOODBYE = "goodbye"
    ACKNOWLEDGEMENT = "acknowledgement"
    SMALL_TALK = "small_talk"


class Person(StrEnum):
    """Who a message is about when it names no customer category. Context only: it never
    becomes a profile value by itself (the dialogue asks or confirms first)."""

    INDIVIDUAL = "individual"  # "for a person", "a personal account", "for me", "myself"
    CHILD = "child"  # "my child", "my six-year-old", "my son", "a minor"


class Unsupported(StrEnum):
    """Customer types the taxonomy has no category for (R7: never mapped to one)."""

    FOREIGN_NATIONAL = "foreign_national"


@dataclass(frozen=True)
class Hint:
    slot: Slot
    value: str
    basis: str  # what triggered it (a code, never the customer's words)


@dataclass(frozen=True)
class Understanding:
    values: dict[Slot, str] = field(default_factory=dict)
    ambiguous: dict[Slot, tuple[str, ...]] = field(default_factory=dict)
    hints: tuple[Hint, ...] = ()
    residency: Residency | None = None
    organisation_unspecified: bool = False
    unsupported: Unsupported | None = None
    intent: Intent | None = None
    topics: tuple[Topic, ...] = ()
    question_kind: QuestionKind = QuestionKind.NONE
    out_of_scope: tuple[str, ...] = ()
    correction_cue: bool = False
    control: Control | None = None
    person: Person | None = None  # who the message is about (context, never a value)
    follow_up: bool = False  # refers back to the conversation ("that", "what about ...")
    separate_request: bool = False  # another / a second / also ... an account
    social: Social | None = None  # a greeting, thanks, goodbye, acknowledgement, small talk

    @property
    def content(self) -> bool:
        """Anything about the account was understood (not just social words)."""
        return bool(
            self.values or self.ambiguous or self.hints or self.residency or self.unsupported
            or self.organisation_unspecified or self.intent or self.topics or self.out_of_scope
            or self.person or self.follow_up or self.separate_request
        )  # fmt: skip

    @property
    def substantive(self) -> bool:
        """Anything beyond a control reply was understood."""
        return self.content or self.social is not None


# --- vocabulary ----------------------------------------------------------------------------------
# Patterns run on normalised text (lower case, punctuation as spaces, "non-resident" ->
# "non resident"). `short` patterns count only in a short reply (<= SHORT_TOKENS words),
# where a bare word such as "current" can only mean the option itself.

SHORT_TOKENS = 4
_ACC = r"(?:account|accounts|a/c|acct|acc)"

ACCOUNT_PATTERNS: dict[AccountType, tuple[str, ...]] = {
    AccountType.SAVINGS: (
        rf"\bsavings? (?:bank )?{_ACC}\b", r"\b(?:nre|nro) savings?\b", rf"\bsb {_ACC}\b",
    ),
    AccountType.CURRENT: (rf"\bcurrent {_ACC}\b", r"\b(?:nre|nro) current\b"),
    AccountType.SALARY: (rf"\bsalary {_ACC}\b", rf"\bpayroll {_ACC}\b"),
    AccountType.FIXED_DEPOSIT: (
        r"\bfixed deposits?\b", r"\bterm deposits?\b", r"\bfds?\b",
    ),
    AccountType.RECURRING_DEPOSIT: (r"\brecurring deposits?\b", r"\brds?\b"),
    AccountType.FCNR_RFC: (
        r"\bfcnr\b", r"\brfc\b", r"\bforeign currency non resident\b",
        r"\bresident foreign currency\b",
    ),
}  # fmt: skip
# "savings or current account": the first option shares the second one's "account".
_COORD = r"(?= (?:or|and|vs|versus|nor) (?:a |an )?(?:savings?|current|salary)\b)"
for _acct, _word in (
    (AccountType.SAVINGS, r"\bsavings?"),
    (AccountType.CURRENT, r"\bcurrent"),
    (AccountType.SALARY, r"\bsalary"),
):
    ACCOUNT_PATTERNS[_acct] += (_word + _COORD,)
ACCOUNT_SHORT: dict[AccountType, tuple[str, ...]] = {
    AccountType.SAVINGS: (r"\bsavings?\b",),
    AccountType.CURRENT: (r"\bcurrent\b",),
    AccountType.SALARY: (r"\bsalary\b",),
}
# An FCNR / RFC deposit is its own option on the form: "FCNR fixed deposit" is FCNR_RFC.
ACCOUNT_SUPPRESSES = {AccountType.FCNR_RFC: AccountType.FIXED_DEPOSIT}

CATEGORY_PATTERNS: dict[CustomerCategory, tuple[str, ...]] = {
    CustomerCategory.NRI_INDIVIDUAL: (
        r"\bnris?\b", r"\bnon resident(?: indians?)?\b", r"\bpios?\b", r"\boci\b",
        r"\bperson of indian origin\b", r"\boverseas citizens?(?: of india)?\b",
    ),
    CustomerCategory.RESIDENT_INDIVIDUAL: (
        r"(?<!non )\bresident (?:indian|individual)s?\b", r"\bindian resident\b",
        r"\bresident of india\b",
    ),
    CustomerCategory.SOLE_PROPRIETORSHIP: (
        r"\bsole proprietor(?:ship)?s?\b", r"\bproprietor(?:ship)?s?\b",
        r"\bproprietary (?:concern|firm)\b", r"\bsole trader\b",
    ),
    CustomerCategory.PARTNERSHIP_FIRM: (r"(?<!liability )\bpartnership(?: firms?)?\b",),
    CustomerCategory.LLP: (r"\bllps?\b", r"\blimited liability partnerships?\b"),
    CustomerCategory.COMPANY: (
        r"\bcompan(?:y|ies)\b", r"\bprivate limited\b", r"\bpublic limited\b",
        r"\bpvt ltd\b", r"\bltd\b",
    ),
    CustomerCategory.TRUST: (
        r"\b(?:a|an|the|our|my|charitable|private|public|religious|family|registered) trusts?\b",
        rf"\btrust (?:{_ACC}|deed|fund)\b", r"\btrustees?\b",
    ),
    CustomerCategory.HUF: (r"\bhufs?\b", r"\bhindu undivided famil(?:y|ies)\b", r"\bkarta\b"),
    CustomerCategory.OTHER_ORGANISATION: (
        r"\bsociet(?:y|ies)\b", r"\bassociations?\b", r"\bclubs?\b", r"\bngos?\b",
        r"\bnon government(?:al)? organi[sz]ations?\b", r"\bself help groups?\b",
    ),
}  # fmt: skip
CATEGORY_SHORT: dict[CustomerCategory, tuple[str, ...]] = {
    CustomerCategory.TRUST: (r"\btrust\b",),
}

FACET_PATTERNS: dict[Slot, dict[str, tuple[str, ...]]] = {
    Slot.NRI_SCHEME: {NriScheme.NRE: (r"\bnre\b",), NriScheme.NRO: (r"\bnro\b",)},
    Slot.HOLDING: {
        Holding.JOINT: (
            r"\bjoint(?:ly)?\b",
            r"\bwith my (?:wife|husband|spouse|mother|father|mum|mom|dad|son|daughter|brother"
            r"|sister|parents?)\b",
        ),
        Holding.SINGLE: (
            r"\bsingle (?:holder|name|applicant)\b", r"\b(?:only|just) me\b",
            r"\bsole (?:holder|applicant)\b", r"\bin my name only\b", r"\bjust myself\b",
        ),
    },
    Slot.MINOR: {
        Minor.YES: (
            r"\bminors?\b", r"\bguardian\b",
            r"\bfor my (?:child|children|son|daughter|kid|kids)\b",
        ),
    },
}  # fmt: skip
FACET_SHORT: dict[Slot, dict[str, tuple[str, ...]]] = {
    Slot.HOLDING: {Holding.SINGLE: (r"\bsingle\b",)},
}

ORGANISATION_UNSPECIFIED = (
    r"\bmy (?:own )?business\b", r"\bbusiness account\b", r"\bfor (?:the|our) business\b",
    r"\bmy (?:shop|firm|startup|start up|enterprise|venture|organi[sz]ation)\b",
    r"\bour (?:firm|organi[sz]ation)\b",
)  # fmt: skip
UNSUPPORTED_PATTERNS = {
    Unsupported.FOREIGN_NATIONAL: (r"\bforeign (?:national|citizen)s?\b", r"\bforeigners?\b"),
}

_LIVE = r"(?:live|living|lives|based|settled|reside|residing|stay|staying|working|work|moved)"
RESIDENCY_PATTERNS = {
    Residency.OUTSIDE_INDIA: (
        rf"\b{_LIVE} (?:abroad|overseas|outside india)\b", r"\bout(?:side)? of india\b",
        rf"\b{_LIVE} (?:in|to) (?:the )?(?:uae|dubai|abu dhabi|usa|us|united states|america"
        r"|uk|united kingdom|england|london|canada|australia|singapore|saudi(?: arabia)?"
        r"|qatar|kuwait|oman|bahrain|germany|new zealand|hong kong|malaysia)\b",
    ),
    Residency.IN_INDIA: (
        rf"\b{_LIVE} in india\b",
        r"\b(?:we|i|she|he|they)(?: are|'re| re| am|'m| m| is|'s| s)?(?: all)? (?:in|based in|from)"
        r" india\b",
    ),
}  # fmt: skip

CONVERSION_PATTERNS = (
    r"\b(?:convert\w*|conversion|chang\w*|switch\w*|redesignat\w*)\b.*\bnro\b",
    r"\bnro\b.*\b(?:convert\w*|conversion)\b",
    r"\bresident account\b.*\bnro\b",
)

TOPIC_PATTERNS: dict[Topic, tuple[str, ...]] = {
    Topic.PHOTOGRAPH: (r"\bphoto(?:graph)?s?\b", r"\bpictures?\b", r"\bpassport size\b"),
    Topic.PAN_CARD_COPY: (
        r"\bpan cards?\b", r"\b(?:photo)?cop(?:y|ies) of (?:my |the |our |its |their )?pan\b",
        r"\bpan (?:copy|photocopy)\b",
    ),
    Topic.PAN_NOT_AVAILABLE: (
        r"\b(?:don't|dont|do not|doesn't|does not) have (?:a |an |any )?pan\b",
        r"\bwithout (?:a )?pan\b", r"\bno pan\b",
    ),
    Topic.PAN: (r"\bpan\b", r"\bpermanent account number\b"),
    Topic.FORM_60: (r"\bform ?6[01]\b",),
    Topic.PASSPORT: (r"\bpassports?\b(?! size)",),
    Topic.VISA_OR_STATUS_PROOF: (r"\bvisas?\b", r"\bwork permit\b", r"\b(?:oci|pio) card\b"),
    Topic.ADDRESS_PROOF: (r"\baddress proof\b", r"\bproof of address\b", r"\butility bills?\b"),
    Topic.IDENTITY_PROOF: (r"\bid proof\b", r"\bidentity proof\b", r"\bproof of identity\b"),
    Topic.AADHAAR: (r"\baadh?aa?r\b",),
    Topic.SIGNATORIES: (r"\bsignator(?:y|ies)\b",),
    Topic.NOMINEE: (r"\bnominees?\b", r"\bnomination\b"),
    Topic.DOCUMENTS: (
        r"\bdocuments?\b", r"\bpaperwork\b", r"\bpapers\b", r"\brequirements?\b",
        r"\bchecklist\b", r"\bwhat (?:do|would|will|does) (?:i|we|my|our|it|they) (?:\w+ )?need\b",
        r"\bhow (?:do|can|would) (?:i|we) open\b", r"\bkyc\b",
    ),
}  # fmt: skip
# More specific PAN topics replace the generic one.
TOPIC_SUPPRESSES = {
    Topic.PAN_CARD_COPY: Topic.PAN,
    Topic.PAN_NOT_AVAILABLE: Topic.PAN,
}
TERM_PATTERNS = (
    r"\bdifference between\b", r"\bwhat (?:is|are) (?:an? |the )?(?:nre|nro|fcnr|rfc|huf|llp)\b",
    r"\bmeaning of\b",
)  # fmt: skip

OUT_OF_SCOPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "loans": (r"\bloans?\b", r"\bmortgage\b", r"\boverdraft\b"),
    "cards": (r"\bcredit cards?\b", r"\bdebit cards?\b"),
    "rates_and_charges": (
        r"\binterest rates?\b", r"\brates? of interest\b", r"\bcharges\b", r"\bfees?\b",
        r"\bminimum balance\b", r"\bexchange rates?\b",
    ),
    "eligibility_or_approval": (
        r"\beligib\w*\b", r"\bapprov\w*\b", r"\breject\w*\b", r"\bcredit score\b", r"\bcibil\b",
    ),
    "other_products": (r"\binsurance\b", r"\bmutual funds?\b", r"\blockers?\b"),
}  # fmt: skip

# Greetings and small talk (checked on the whole, normalised message).
SOCIAL_PATTERNS: dict[Social, tuple[str, ...]] = {
    Social.GREETING: (
        r"^(?:hi+|hii+|hello+|hey+|hiya|heya|yo+|howdy|greetings|namaste|hola)\b",
        r"^good (?:morning|afternoon|evening|day)\b",
    ),
    Social.THANKS: (
        r"\b(?:thanks?|thank you|thankyou|thx|ty|cheers|appreciate(?: it| that)?)\b",
    ),
    Social.GOODBYE: (
        r"\b(?:bye|goodbye|good bye|see (?:you|ya)|that'?s all|that is all|have a (?:nice|good) day)\b",
    ),
    Social.SMALL_TALK: (
        r"\bhow are you\b", r"\bhow'?s it going\b", r"\bwhat'?s up\b", r"\bwhats up\b",
        r"\bwho are you\b", r"\bare you (?:a )?(?:bot|human|robot)\b",
    ),
    Social.ACKNOWLEDGEMENT: (
        r"^(?:cool|great|nice|got it|alright|all right|perfect|understood|noted|fine|awesome"
        r"|makes sense|good)\b",
    ),
}  # fmt: skip

# Conversational context (owner request: context-aware follow-ups). These never set a
# profile value: the dialogue decides whether to ask, confirm or keep the profile.
PERSON_PATTERNS: dict[Person, tuple[str, ...]] = {
    Person.CHILD: (
        r"\b(?:my|our|a|the|his|her) (?:\w+ )?(?:child|children|son|daughter|kid|kids|baby)\b",
        r"\b\w+ (?:year|yr|month) olds?\b", r"\bminors?\b",
    ),
    Person.INDIVIDUAL: (
        r"\b(?:a|one) person\b", r"\bpersonal\b", r"\bindividual\b", r"\bfor (?:me|myself)\b",
        r"\bmyself\b", r"\bmy own\b", r"\bjust me\b",
    ),
}  # fmt: skip
REFERENCE_PATTERNS = (
    r"\bwhat about\b", r"\bhow about\b", r"\bwhat if\b", r"\b(?:and|but) (?:if|what)\b",
    r"\b(?:does|do|would|will|is|are|can) (?:that|this|it|these|those|they)\b",
    r"\b(?:for|about|of|with) (?:that|this|it|those|these)\b", r"\bapply to\b", r"\bthe same\b",
)  # fmt: skip
SEPARATE_WORDS = (
    r"\balso\b", r"\btoo\b", r"\bas well\b", r"\banother\b", r"\bsecond\b", r"\bseparate\b",
    r"\badditional\b", r"\bone more\b",
)  # fmt: skip
ACCOUNT_OR_OPEN = (r"\baccounts?\b", r"\bopen(?:ing)?\b")

CORRECTION_CUES = (
    r"\bactually\b", r"\bsorry\b", r"\bi meant\b", r"\bi mean\b", r"\bcorrection\b",
    r"\binstead\b", r"\brather\b", r"\bchange (?:it|that|this)\b", r"^no\b", r"^oops\b",
    r"\bnot (?:a |an |the )?\w+ but\b",
)  # fmt: skip

CONTROL_PATTERNS: dict[Control, tuple[str, ...]] = {
    Control.NOT_SURE: (
        r"\bnot sure\b", r"\bunsure\b", r"\b(?:don't|dont|do not) know\b", r"\bno idea\b",
        r"\bnot certain\b", r"\bdunno\b",
    ),
    Control.SKIP: (r"^skip(?: (?:this|it|that|this one))?$", r"^pass$", r"^next(?: question)?$"),
    Control.EXPLAIN: (
        r"\bwhat does (?:this|that|it) mean\b", r"\bwhat do you mean\b",
        r"\bwhat(?: is|'s|s) (?:this|that)\b", r"\bexplain\b",
        r"\b(?:don't|dont|do not) understand\b", r"\bdifference between\b",
    ),
    Control.YES: (r"^(?:yes|yeah|yep|yup|correct|right|that's right|thats right|sure|ok|okay|y)$",),
    Control.NO: (r"^(?:no|nope|not really|wrong|incorrect|n)$",),
}  # fmt: skip

_NEGATION = re.compile(
    r"\b(?:not|isn't|isnt|never|nor|neither)\s+(?:(?:a|an|the|for|my|our|in|as|from)\s+){0,2}$"
)


# --- matching ------------------------------------------------------------------------------------


def normalise(text: str) -> str:
    t = text.lower().replace("’", "'").replace("non-resident", "non resident")
    t = re.sub(r"[^a-z0-9/']+", " ", t)
    return " ".join(t.split())


def _found(patterns: tuple[str, ...], text: str) -> bool:
    """A pattern counts unless it is directly negated ("not a company")."""
    for p in patterns:
        for m in re.finditer(p, text):
            if not _NEGATION.search(text[: m.start()]):
                return True
    return False


def _matches(
    table: dict[str, tuple[str, ...]] | dict[AccountType, tuple[str, ...]]
    | dict[CustomerCategory, tuple[str, ...]],
    short: dict[str, tuple[str, ...]] | dict[AccountType, tuple[str, ...]]
    | dict[CustomerCategory, tuple[str, ...]],
    text: str,
    is_short: bool,
) -> list[str]:  # fmt: skip
    found = [str(k) for k, pats in table.items() if _found(pats, text)]
    if is_short:
        found += [str(k) for k, pats in short.items() if str(k) not in found and _found(pats, text)]
    return found


def understand(message: str) -> Understanding:
    """Deterministic reading of one (already screened) message."""
    text = normalise(message)
    if not text:
        return Understanding()
    is_short = len(text.split()) <= SHORT_TOKENS
    values: dict[Slot, str] = {}
    ambiguous: dict[Slot, tuple[str, ...]] = {}

    def record(slot: Slot, found: list[str]) -> None:
        if len(found) == 1:
            values[slot] = found[0]
        elif len(found) > 1:
            ambiguous[slot] = tuple(sorted(found))

    accounts = _matches(ACCOUNT_PATTERNS, ACCOUNT_SHORT, text, is_short)
    for winner, loser in ACCOUNT_SUPPRESSES.items():
        if winner in accounts and loser in accounts:
            accounts.remove(loser)
    record(Slot.ACCOUNT_TYPE, accounts)
    record(Slot.CUSTOMER_CATEGORY, _matches(CATEGORY_PATTERNS, CATEGORY_SHORT, text, is_short))
    for slot, table in FACET_PATTERNS.items():
        record(slot, _matches(table, FACET_SHORT.get(slot, {}), text, is_short))

    residency = next((r for r, pats in RESIDENCY_PATTERNS.items() if _found(pats, text)), None)
    organisation = _found(ORGANISATION_UNSPECIFIED, text)
    unsupported = next((u for u, pats in UNSUPPORTED_PATTERNS.items() if _found(pats, text)), None)
    has_category = Slot.CUSTOMER_CATEGORY in values or Slot.CUSTOMER_CATEGORY in ambiguous
    if has_category:
        organisation, unsupported = False, None
    hints: list[Hint] = []
    if not has_category and unsupported is None:
        if residency is Residency.OUTSIDE_INDIA:
            hints.append(Hint(Slot.CUSTOMER_CATEGORY, CustomerCategory.NRI_INDIVIDUAL, "abroad"))
        elif residency is Residency.IN_INDIA and not organisation:
            hints.append(
                Hint(Slot.CUSTOMER_CATEGORY, CustomerCategory.RESIDENT_INDIVIDUAL, "in_india")
            )
        elif Slot.NRI_SCHEME in values or Slot.NRI_SCHEME in ambiguous:
            # NRE / NRO accounts are the sections of the NRI form; still only a hint (R7).
            hints.append(
                Hint(Slot.CUSTOMER_CATEGORY, CustomerCategory.NRI_INDIVIDUAL, "nri_scheme")
            )

    intent = (
        Intent.RESIDENT_TO_NRO_CONVERSION
        if any(re.search(p, text) for p in CONVERSION_PATTERNS)
        else None
    )
    topics = [t for t, pats in TOPIC_PATTERNS.items() if _found(pats, text)]
    for specific, generic in TOPIC_SUPPRESSES.items():
        if specific in topics and generic in topics:
            topics.remove(generic)
    term = any(re.search(p, text) for p in TERM_PATTERNS)
    if term:
        kind = QuestionKind.TERM
    elif [t for t in topics if t is not Topic.DOCUMENTS]:
        kind = QuestionKind.ITEM
    elif topics:
        kind = QuestionKind.CHECKLIST
    else:
        kind = QuestionKind.NONE
    out_of_scope = tuple(k for k, pats in OUT_OF_SCOPE_PATTERNS.items() if _found(pats, text))
    correction = any(re.search(p, text) for p in CORRECTION_CUES)
    control = next(
        (c for c, pats in CONTROL_PATTERNS.items() if any(re.search(p, text) for p in pats)),
        None,
    )
    if control is Control.NOT_SURE and not is_short and (values or topics):
        control = None  # "not sure which documents I need for an NRE account" is a question
    # Who the message is about, when no category is named ("for my child", "a person").
    person = None
    if not has_category:
        person = next((p for p, pats in PERSON_PATTERNS.items() if _found(pats, text)), None)
    follow_up = _found(REFERENCE_PATTERNS, text)
    separate = _found(SEPARATE_WORDS, text) and _found(ACCOUNT_OR_OPEN, text)
    social = next((s for s, pats in SOCIAL_PATTERNS.items() if _found(pats, text)), None)
    return Understanding(
        values=values,
        ambiguous=ambiguous,
        hints=tuple(hints),
        residency=residency,
        organisation_unspecified=organisation,
        unsupported=unsupported,
        intent=intent,
        topics=tuple(topics),
        question_kind=kind,
        out_of_scope=out_of_scope,
        correction_cue=correction,
        control=control,
        person=person,
        follow_up=follow_up,
        separate_request=separate,
        social=social,
    )
