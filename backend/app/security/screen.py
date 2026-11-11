"""Deterministic input screening (M3 S3; M3_PLAN §6, docs/06).

`screen_text` is applied to every piece of user text before it is stored in workflow
state or sent to any model: the initial question (`app.graph.inputs.start_input`),
every clarification answer (`resume_command`), and again inside the graph by the
`input_security` and `clarify` nodes (defence in depth).

What it checks:
- Input hygiene: empty input, length, invalid encoding (lone surrogates), control
  characters (block); invisible or bidirectional formatting characters (review).
- A NARROW list of instruction-override and system-prompt-extraction phrases (block) and
  role-override phrases (review). Questions *about* system prompts or security are not
  matched: only imperative attempts to override or reveal instructions are.
- Personal data. Government and financial identifiers block the request: PAN, Aadhaar
  (12 digits passing the Verhoeff check), passport-like numbers, card numbers (Luhn) and
  long digit runs next to account / IFSC wording. Contact details (email, phone) are
  redacted to `[REDACTED_EMAIL]` / `[REDACTED_PHONE]` and the request proceeds.

What it returns: an action (allow | redact | review | block), category codes and counts,
and the NFKC-normalised text with email/phone redacted. The text is withheld (None) when
an identifier was found or the input is not valid text. It never returns or logs a match.

Limits (stated in docs/06 and the M3 plan): this is a best-effort pattern filter. It does
NOT guarantee that all personal data is found, and it is NOT a prompt-injection defence:
Prompt Guard is M4, and the real controls are the synthetic-data-only policy, evidence
delimiting and the deterministic output validator.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

Action = Literal["allow", "redact", "review", "block"]
_RANK: dict[str, int] = {"allow": 0, "redact": 1, "review": 2, "block": 3}

DEFAULT_MAX_CHARS = 2000

# --- categories -------------------------------------------------------------------------
BLOCK_HYGIENE = ("empty_input", "too_long", "invalid_encoding", "control_characters")
BLOCK_INJECTION = ("override_instruction", "system_prompt_extraction")
REVIEW_INJECTION = ("role_override", "hidden_formatting")
BLOCK_PII = ("pan", "aadhaar", "passport", "card_number", "account_number")
REDACT_PII = ("email", "phone")

# --- input hygiene ----------------------------------------------------------------------
_ALLOWED_CONTROL = {"\n", "\r", "\t"}
# Invisible or direction-overriding characters. ZWNJ/ZWJ (U+200C/U+200D) and LRM/RLM
# (U+200E/U+200F) are deliberately NOT here: Indic scripts and Urdu use them normally.
# Kept: zero-width space, word joiner and invisible operators, BOM, and the bidi
# embedding/override/isolate controls ("Trojan Source").
_HIDDEN = re.compile("[\u200b\u2060-\u2064\ufeff\u202a-\u202e\u2066-\u2069]")

# --- narrow injection phrases (matched on NFKC-normalised, case-folded text) ---------------
_OVERRIDE = [
    # "ignore (all) (the) previous/your/system ... instructions|prompt". Deliberately not
    # "rules" or "directions": "can a bank ignore the RBI directions?" is a real question.
    re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\s+(all\s+|any\s+|of\s+)?(the\s+)?"
        r"(previous|prior|above|earlier|your|system|these|those)\s+"
        r"(\w+\s+)?(instructions?|prompts?)\b"
    ),
    re.compile(r"\bnew (system )?instructions?\s*:"),
]
_EXTRACTION = [
    re.compile(
        r"\b(reveal|print|show|display|repeat|output|leak|dump|tell me|give me|what (is|are))\b"
        r"[\w\s,'-]{0,20}?\b(your|the)\s+(system|hidden|initial|original|developer)\s+"
        r"(prompt|instructions?|message)\b"
    ),
]
_ROLE = [
    re.compile(r"\byou are (now|no longer)\b"),
    re.compile(r"\b(act|behave|respond) as (an? )?(unrestricted|unfiltered|jailbroken|evil)\b"),
    re.compile(r"\b(developer|god|dan|jailbreak) mode\b"),
    re.compile(r"\bpretend (that )?you (are|have) no (rules|restrictions|guidelines)\b"),
]
# A question ABOUT system prompts ("what is a system prompt?") must stay allowed: the
# extraction pattern needs "your"/"the" + a qualifier + prompt; "what is the system
# prompt of this tool" is an extraction attempt, "what is a system prompt" is not.

# --- personal data (matched on NFKC-normalised text) ----------------------------------------
# PAN: 5 letters (the 4th is the holder-type code), 4 digits, 1 letter.
_PAN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]{3}[PCHFATBLJGpchfatbljg][A-Za-z][0-9]{4}[A-Za-z](?![A-Za-z0-9])"
)
_AADHAAR = re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")
_PASSPORT = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][0-9]{7}(?![A-Za-z0-9])")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_ACCOUNT_CONTEXT = re.compile(r"\b(a/?c|acct|account|ifsc)\b", re.IGNORECASE)
_LONG_DIGITS = re.compile(r"(?<!\d)\d{9,18}(?!\d)")
_ACCOUNT_WINDOW = 40  # characters before the digits that may hold the account wording
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_PHONE = re.compile(
    # Indian mobile, optionally +91 / 0091 / 91 / 0 prefixed, optionally split 5-5
    r"(?<![\w+])(?:(?:\+|00)?91[ -]?|0)?[6-9]\d{4}[ -]?\d{5}(?!\d)"
    # any other number written in international form: + country code and 7-14 digits
    r"|(?<![\w+])\+\d{1,3}[ -]?\d(?:[ -]?\d){6,13}(?!\d)"
)

# Verhoeff tables (Aadhaar's check digit scheme).
_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(digits: str) -> str:
    """The digit that makes `digits + d` Verhoeff-valid (used to build synthetic test data)."""
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _D[c][_P[(i + 1) % 8][int(ch)]]
    return str(_INV[c])


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass(frozen=True)
class ScreenResult:
    """The outcome of screening one text. `text` is the (possibly redacted) text that may
    be used downstream; it is None when the input is blocked."""

    action: Action
    categories: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    text: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == "block"

    @property
    def sensitive(self) -> bool:
        """A government or financial identifier was found: nothing may be stored or sent."""
        return any(c in BLOCK_PII for c in self.categories)

    @property
    def unusable(self) -> bool:
        """The input cannot be processed at all (empty, too long, not valid text)."""
        return any(c in ("empty_input", "too_long", "invalid_encoding") for c in self.categories)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _pii(text: str) -> Counter[str]:
    found: Counter[str] = Counter()
    found["pan"] += len(_PAN.findall(text))
    for m in _AADHAAR.finditer(text):
        if verhoeff_valid(_digits(m.group())):
            found["aadhaar"] += 1
    found["passport"] += len(_PASSPORT.findall(text))
    for m in _CARD.finditer(text):
        d = _digits(m.group())
        if 13 <= len(d) <= 19 and luhn_valid(d):
            found["card_number"] += 1
    # A long digit run counts as an account number only shortly after account/IFSC wording
    # (most KYC questions say "account"), and phone numbers are taken out first.
    without_phones = _PHONE.sub(" ", text)
    for m in _LONG_DIGITS.finditer(without_phones):
        window = without_phones[max(0, m.start() - _ACCOUNT_WINDOW) : m.start()]
        if _ACCOUNT_CONTEXT.search(window):
            found["account_number"] += 1
    return +found


def screen_text(text: object, *, max_chars: int = DEFAULT_MAX_CHARS) -> ScreenResult:
    """Screen one user text. Never raises on content; a non-string is blocked."""
    if not isinstance(text, str):
        return ScreenResult("block", ("invalid_encoding",), {"invalid_encoding": 1})
    hits: Counter[str] = Counter()
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return ScreenResult("block", ("invalid_encoding",), {"invalid_encoding": 1})
    if not text.strip():
        return ScreenResult("block", ("empty_input",), {"empty_input": 1})
    if len(text) > max_chars:
        return ScreenResult("block", ("too_long",), {"too_long": 1})
    controls = sum(
        1 for ch in text if unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL
    )
    if controls:
        hits["control_characters"] += controls
    hidden = len(_HIDDEN.findall(text))
    if hidden:
        hits["hidden_formatting"] += hidden

    normalised = unicodedata.normalize("NFKC", _HIDDEN.sub("", text))
    folded = " ".join(normalised.casefold().split())
    for name, patterns in (
        ("override_instruction", _OVERRIDE),
        ("system_prompt_extraction", _EXTRACTION),
        ("role_override", _ROLE),
    ):
        n = sum(len(p.findall(folded)) for p in patterns)
        if n:
            hits[name] += n

    hits.update(_pii(normalised))
    emails = len(_EMAIL.findall(normalised))
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", normalised)
    phones = len(_PHONE.findall(redacted))
    redacted = _PHONE.sub("[REDACTED_PHONE]", redacted)
    if emails:
        hits["email"] += emails
    if phones:
        hits["phone"] += phones

    categories = tuple(sorted(hits))
    counts = dict(sorted(hits.items()))
    # Identifier-bearing text is never passed on. Otherwise the redacted text is, even for
    # a block or review, so a BLOCKED report can be produced without contact details.
    passable = None if any(c in hits for c in BLOCK_PII) else redacted
    if any(c in hits for c in (*BLOCK_HYGIENE, *BLOCK_INJECTION, *BLOCK_PII)):
        return ScreenResult("block", categories, counts, passable)
    if any(c in hits for c in REVIEW_INJECTION):
        return ScreenResult("review", categories, counts, passable)
    if emails or phones:
        return ScreenResult("redact", categories, counts, redacted)
    return ScreenResult("allow", (), {}, normalised)


def strictest(*actions: Action) -> Action:
    """The most restrictive of several actions (block > review > redact > allow)."""
    return max(actions, key=lambda a: _RANK[a]) if actions else "allow"
