"""A2: the HDFC-only retrieval evaluation set covers what the owner asked for, agrees with
the coverage map, and never manufactures gold evidence; the scoring is exact."""

from collections import Counter

from app.assistant.coverage import coverage
from app.assistant.evaluation import EvalCase, evaluate_case, load_cases
from app.assistant.retrieval import AssistantRetriever
from app.assistant.taxonomy import AccountType, CustomerCategory, EvidenceScope
from tests.unit.test_assistant_retrieval import FakeRetriever, payload


def test_the_set_has_about_25_cases_with_required_coverage() -> None:
    cases = load_cases()
    assert 25 <= len(cases) <= 35
    tags = Counter(t for c in cases for t in c.tags)
    for required in ("nre_nro", "llp", "partnership", "company", "pan_number_vs_card",
                     "photograph", "unsupported", "insufficient_evidence"):  # fmt: skip
        assert tags[required] >= 1, required
    keys = {(c.category, c.account_type) for c in cases}
    assert (CustomerCategory.NRI_INDIVIDUAL, AccountType.SAVINGS) in keys
    assert (CustomerCategory.NRI_INDIVIDUAL, AccountType.CURRENT) in keys
    assert (CustomerCategory.LLP, AccountType.CURRENT) in keys
    resident = [
        c
        for c in cases
        if c.category is CustomerCategory.RESIDENT_INDIVIDUAL and c.scope == "opening"
    ]
    assert resident and all(c.expected == "no_evidence" for c in resident)


def test_cases_agree_with_the_coverage_map() -> None:
    cov = coverage()
    for c in load_cases():
        if c.scope != "opening":
            continue
        assert c.category is not None and c.account_type is not None
        covered = cov.cell(c.category, c.account_type).covered
        if c.expected_reason == "not_established":
            assert not covered, c.id
        else:
            assert covered, c.id  # evidence and absent-in-scope cases are covered cells


def test_no_evidence_cases_have_no_gold_and_evidence_cases_do() -> None:
    for c in load_cases():
        if c.expected == "no_evidence":
            assert c.gold == () and c.expected_reason in ("not_established", "absent_in_scope")
        else:
            assert c.gold


def test_scoring_is_exact() -> None:
    gold = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000009")
    case = EvalCase(id="t", query="NRE savings", scope="opening",
                    category=CustomerCategory.NRI_INDIVIDUAL, account_type=AccountType.SAVINGS,
                    expected="evidence", gold=gold, source_type="opening_form")  # fmt: skip
    fake = FakeRetriever(*(payload("hdfc_nri_account_opening") for _ in range(5)))
    res = evaluate_case(AssistantRetriever(fake), case)
    assert res.recall == 0.5 and res.first_gold_rank == 2 and res.metadata_complete


def test_a_not_established_case_passes_only_when_no_search_runs() -> None:
    case = EvalCase(id="t", query="resident savings", scope="opening",
                    category=CustomerCategory.RESIDENT_INDIVIDUAL, account_type=AccountType.SAVINGS,
                    expected="no_evidence", expected_reason="not_established", gold=(),
                    source_type="none")  # fmt: skip
    fake = FakeRetriever(payload("hdfc_nri_account_opening"))
    res = evaluate_case(AssistantRetriever(fake), case)
    assert res.passed_no_evidence is True and not res.retrieval.searched and fake.calls == []
    assert res.retrieval.scope is EvidenceScope.OPENING
