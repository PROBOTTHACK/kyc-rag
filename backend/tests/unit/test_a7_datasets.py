"""A7.1: the evaluation datasets are valid, synthetic, and in step with the coverage map.

The full A7 suites run on the development stack (`make a7-eval`); this unit test keeps the
datasets honest in every `make test`, and runs the scripted conversations against the
in-memory store as a fast deterministic regression check.
"""

import json
from collections import Counter

from app.assistant.a7.datasets import (
    CATEGORIES,
    PROFILES_FILE,
    load_conversations,
    load_memory_cases,
    load_pii,
    load_profiles,
)
from app.assistant.a7.driver import run_turns
from app.assistant.a7.generate_profiles import generate
from app.assistant.checklist import ChecklistService
from app.assistant.service import AssistantService
from app.security.screen import screen_text
from tests.unit.test_assistant_api import MemoryStore
from tests.unit.test_assistant_checklist import FakeChunks


def test_the_profiles_file_is_generated_from_the_coverage_map() -> None:
    expected = "".join(
        json.dumps(p.model_dump(mode="json"), sort_keys=True) + "\n" for p in generate()
    )
    assert (
        PROFILES_FILE.read_text(encoding="utf-8") == expected
    ), "run `python -m app.assistant.a7.generate_profiles`"


def test_the_profiles_cover_every_cell_and_the_conversion_intent() -> None:
    profiles = load_profiles()
    kinds = Counter(p.kind for p in profiles)
    covered_cells = {(p.customer_category, p.account_type) for p in profiles if p.kind == "covered"}
    assert len(covered_cells) == 26
    complete = [p for p in profiles if p.kind == "covered" and "NOT_SURE" not in p.facets.values()]
    assert len(complete) == 55
    assert kinds["not_established"] == 28
    assert kinds["conversion"] >= 10 and kinds["conversion_near_miss"] >= 5
    assert len({p.id for p in profiles}) == len(profiles)


def test_the_conversation_set_covers_every_category() -> None:
    s = load_conversations()
    counts = Counter(c.category for c in s.conversations)
    assert set(counts) == set(CATEGORIES)
    assert all(n >= 8 for n in counts.values()), counts
    assert len(s.conversations) >= 70


def test_scripted_messages_hold_no_identifiers() -> None:
    """Synthetic only: no scripted message is refused by screening unless it says so."""
    for c in load_conversations().conversations:
        for t in c.turns:
            if t.say is not None and not t.expect.blocked:
                assert not screen_text(t.say).sensitive, (c.id, t.say)
    for case in load_memory_cases().cases:
        for t in case.turns:
            if t.say is not None and "PAN is" not in t.say:
                assert not screen_text(t.say).sensitive, (case.id, t.say)


def test_the_pii_set_is_synthetic_and_balanced() -> None:
    rows = load_pii()
    assert len({r.id for r in rows}) == len(rows)
    ids = [r for r in rows if r.expect != "allow"]
    controls = [r for r in rows if r.expect == "allow"]
    assert len(ids) >= 30 and len(controls) >= 10
    for r in ids:
        assert r.secret and r.secret in r.message, r.id
    for r in controls:
        assert r.secret is None
    assert {r.category for r in ids} >= {
        "pan", "aadhaar", "passport", "card_number", "account_number", "email", "phone",
    }  # fmt: skip


def test_the_memory_cases_load() -> None:
    assert len(load_memory_cases().cases) >= 15


async def test_the_conversations_pass_on_the_in_memory_stack() -> None:
    store = MemoryStore()

    def make() -> AssistantService:
        return AssistantService(store, lambda: ChecklistService(FakeChunks()), None)

    failures = []
    for c in load_conversations().conversations:
        run = await run_turns(make(), c.turns, restart=make)
        failures += [(c.id, t.index, t.failures) for t in run.turns if t.failures]
    assert failures == []
