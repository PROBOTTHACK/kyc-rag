"""M4 S4.1: the dev evaluation harness end to end on the stored corpus, WITHOUT a model.

Every dev case's premises are built from its stored Postgres chunks; the rules-only ablation
runs over all 55 cases. With no scorer nothing can be supported, so the false-support rate
and MANDATORY false positives must be zero: the safety floor the NLI must not break.
"""

import pytest

from app.config import get_settings
from app.db.session import create_sync_engine, create_sync_sessionmaker
from app.graph.corpus import PostgresCorpusReader
from app.verification.decide import Thresholds
from app.verification.evaluation import compute_metrics, load_dev, predict, premises_for

pytestmark = pytest.mark.integration


def test_rules_only_over_the_whole_dev_set_is_safe_by_construction() -> None:
    settings = get_settings()
    corpus = PostgresCorpusReader(create_sync_sessionmaker(create_sync_engine(settings)))
    cases, sha = load_dev()
    premises = {c.id: premises_for(c, corpus) for c in cases}
    assert all(premises[c.id] and all(p.windows for p in premises[c.id]) for c in cases)
    roles = {cid: {p.evidence_role for p in ps} for cid, ps in premises.items()}
    unused = Thresholds(0.9, 0.9, "rules-only")
    preds = [
        predict(c, premises[c.id], scorer=None, thresholds=unused, max_pairs=400,
                mode="rules_only")  # fmt: skip
        for c in cases
    ]
    m = compute_metrics(cases, preds, roles)
    assert m.n == 55 and len(sha) == 64
    assert m.false_support_rate == 0 and m.recall["entailed"] == 0
    assert m.mandatory_false_positives == 0 and m.obligation_from_form_errors == 0
    # only rule-based contradictions (a negated obligation naming the item) can be predicted
    assert set(m.verdicts) <= {"not_verified", "contradicted", "insufficient"}
