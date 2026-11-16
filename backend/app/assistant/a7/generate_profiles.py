"""Generate data/eval/a7_coverage_profiles.jsonl from the coverage map (never by hand).

    python -m app.assistant.a7.generate_profiles          (run in backend/)

For every covered cell: one profile per combination of its material facet values, plus one
"I'm not sure" variant per facet. Every not-established cell. The resident-to-NRO conversion
phrasings and near misses (the only hand-written lines: customer wording, no expectations
beyond "conversion" / "not conversion").
"""

from __future__ import annotations

import itertools
import json

from app.assistant.a7.datasets import PROFILES_FILE, CoverageProfile
from app.assistant.coverage import coverage
from app.assistant.profile import SLOT_VALUES, slot_for_facet
from app.assistant.taxonomy import AccountType, CustomerCategory

CONVERSION_PHRASES = (
    "I want to convert my resident savings account to NRO",
    "please convert my account to an NRO account",
    "I moved to Dubai, how do I change my savings account to NRO?",
    "conversion of my resident account to NRO",
    "I am moving abroad and need to switch my account to NRO",
    "can I redesignate my savings account as NRO?",
    "change my account to NRO",
    "convert resident account to NRO account",
    "I have relocated to Canada, what is needed to convert my account to NRO?",
    "switching my existing account to NRO",
    "what do I need for converting to an NRO account?",
    "I want my resident account changed into an NRO account",
)
CONVERSION_NEAR_MISSES = (
    "I want to open an NRO account",
    "what is an NRO account?",
    "NRE or NRO, which one should I open?",
    "I want to convert currency",
    "open a savings account for an NRI",
    "I live in Dubai and want an NRO savings account",
)


def generate() -> list[CoverageProfile]:
    cov = coverage()
    out: list[CoverageProfile] = []
    for cat in CustomerCategory:
        for acct in AccountType:
            cell = cov.cell(cat, acct)
            base = {
                "customer_category": cat.value,
                "account_type": acct.value,
                "state": cell.state.value,
                "sources": tuple(cell.sources),
            }
            if not cell.covered:
                out.append(
                    CoverageProfile(
                        id=f"ne-{cat.value}-{acct.value}", kind="not_established", **base
                    )
                )
                continue
            slots = [slot_for_facet(f) for f in cell.material_facets]
            values = [[v.value for v in SLOT_VALUES[s]] for s in slots]
            for combo in itertools.product(*values) if slots else [()]:
                facets = {s.value: v for s, v in zip(slots, combo, strict=True)}
                suffix = "-".join(combo) or "base"
                out.append(
                    CoverageProfile(
                        id=f"cov-{cat.value}-{acct.value}-{suffix}",
                        kind="covered",
                        facets=facets,
                        **base,
                    )
                )
            for slot in slots:  # "I'm not sure" on each facet in turn
                facets = {s.value: v[0] for s, v in zip(slots, values, strict=True)}
                facets[slot.value] = "NOT_SURE"
                out.append(
                    CoverageProfile(
                        id=f"cov-{cat.value}-{acct.value}-notsure-{slot.value}",
                        kind="covered",
                        facets=facets,
                        **base,
                    )
                )
    conversion = cov.conversion
    for n, text in enumerate(CONVERSION_PHRASES, 1):
        out.append(
            CoverageProfile(
                id=f"conv-{n:02d}",
                kind="conversion",
                state="CONVERSION_ONLY",
                sources=tuple(conversion.sources),
                message=text,
            )
        )
    for n, text in enumerate(CONVERSION_NEAR_MISSES, 1):
        out.append(
            CoverageProfile(id=f"conv-miss-{n:02d}", kind="conversion_near_miss", message=text)
        )
    return out


def main() -> None:
    profiles = generate()
    PROFILES_FILE.write_text(
        "".join(json.dumps(p.model_dump(mode="json"), sort_keys=True) + "\n" for p in profiles),
        encoding="utf-8",
        newline="\n",
    )
    kinds: dict[str, int] = {}
    for p in profiles:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
    print(f"wrote {PROFILES_FILE} {kinds}")


if __name__ == "__main__":
    main()
