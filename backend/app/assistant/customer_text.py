"""The internal-language guard for customer-facing text (A4 language pass; shared from the
fallback "explain + suggest" change).

Customer-facing wording never names internal stages, rules or implementation details. Data
files that hold customer wording (the checklist catalogue, the coverage map's explanations)
run this check on load and refuse to load otherwise.
"""

import re

INTERNAL = re.compile(
    r"\bA[0-9]\b|§|schema|catalogue|chunk|corpus|ruling|stored text|\brule \d", re.IGNORECASE
)


def check_customer_text(owner: str, *texts: str | None) -> None:
    for text in texts:
        if text and INTERNAL.search(text):
            raise ValueError(f"{owner}: customer-facing text names internal details: {text!r}")
